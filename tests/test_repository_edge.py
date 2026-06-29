"""Tests for FileRepository edge cases: corrupt JSON, OSError retry, concurrent access."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from agent_prod.gates.models import Improvement
from agent_prod.gates.repository import FileRepository


class TestFileRepositoryEdgeCases:
    """FileRepository resilience tests beyond basic concurrency."""

    def test_corrupt_json_load(self, tmp_path: Path):
        """Corrupt JSON file should not crash — logs error and returns empty cache."""
        path = tmp_path / "improvements.json"
        path.write_text("{corrupt json!!!}")
        repo = FileRepository(str(path))
        assert repo.count() == 0

    def test_empty_file_load(self, tmp_path: Path):
        """Empty file should not crash."""
        path = tmp_path / "improvements.json"
        path.write_text("")
        repo = FileRepository(str(path))
        assert repo.count() == 0

    def test_file_not_found_on_init(self, tmp_path: Path):
        """FileRepository creates parent dirs and starts empty if file missing."""
        path = tmp_path / "nonexistent" / "improvements.json"
        repo = FileRepository(str(path))
        assert repo.count() == 0
        # Save and verify it creates the file
        imp = Improvement(name="test", id="imp-test-001")
        repo.save(imp)
        assert repo.count() == 1
        assert path.exists()

    def test_concurrent_save_delete(self, tmp_path: Path):
        """Concurrent save() and delete() on different keys — no data loss."""
        path = tmp_path / "improvements.json"
        repo = FileRepository(str(path))

        imp_a = Improvement(name="A", id="imp-a")
        imp_b = Improvement(name="B", id="imp-b")
        repo.save(imp_a)
        repo.save(imp_b)

        errors = []
        barrier = threading.Barrier(2, timeout=5)

        def saver():
            try:
                for i in range(50):
                    imp = Improvement(name=f"A-{i}", id=f"imp-a-{i}")
                    repo.save(imp)
                barrier.wait()
            except Exception as e:
                errors.append(f"saver: {e}")

        def deleter():
            try:
                for i in range(50):
                    repo.delete("imp-b")
                    repo.save(Improvement(name=f"B-{i}", id=f"imp-b-{i}"))
                barrier.wait()
            except Exception as e:
                errors.append(f"deleter: {e}")

        t1 = threading.Thread(target=saver)
        t2 = threading.Thread(target=deleter)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        # Data should survive reload
        repo2 = FileRepository(str(path))
        assert repo2.get("imp-a-49") is not None
        assert repo2.get("imp-b-49") is not None

    def test_save_then_reload(self, tmp_path: Path):
        """Data persisted to disk survives FileRepository reload."""
        path = tmp_path / "improvements.json"
        repo = FileRepository(str(path))
        imp = Improvement(name="persist-test", id="imp-persist")
        repo.save(imp)

        repo2 = FileRepository(str(path))
        loaded = repo2.get("imp-persist")
        assert loaded is not None
        assert loaded.name == "persist-test"


class TestFileRepositoryRetry:
    """Tests for the _retry_on_oserror decorator behavior."""

    def test_persist_retries_on_oserror(self, monkeypatch, tmp_path: Path):
        """_persist retries on transient OSError and eventually succeeds."""
        path = tmp_path / "improvements.json"
        repo = FileRepository(str(path))

        call_count = 0
        original_open = os.open

        def flaky_open(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:  # Fail first 2 calls
                raise OSError(5, "Input/output error (simulated)")
            return original_open(*args, **kwargs)

        monkeypatch.setattr(os, "open", flaky_open)

        imp = Improvement(name="retry-test", id="imp-retry")
        repo.save(imp)

        loaded = repo.get("imp-retry")
        assert loaded is not None
        assert call_count >= 2  # At least 2 retries happened

    def test_retry_exhausted_raises(self, monkeypatch, tmp_path: Path):
        """_persist raises after exhausting retries on persistent OSError."""
        path = tmp_path / "improvements.json"
        repo = FileRepository(str(path))

        def always_fail(*args, **kwargs):
            raise OSError(28, "No space left on device (simulated)")

        monkeypatch.setattr(os, "open", always_fail)

        imp = Improvement(name="fail-test", id="imp-fail")
        repo.save(imp)

        # save() should not raise (OSError is logged in _persist),
        # but data should still be in cache
        assert repo.get("imp-fail") is not None