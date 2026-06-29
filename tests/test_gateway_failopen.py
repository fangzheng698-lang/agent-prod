"""Tests for gateway fail-open behavior (R3 fix)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_prod.gateway.gateway import QualityGateGateway
from agent_prod.gates.models import Improvement, ImprovementStatus
from agent_prod.gates.engine import QualityGateEngine


class TestGatewayFailOpen:
    """Verify validate() error handling after R3 fix.

    Default (fail_open_on_error=False): unexpected exceptions → REJECTED.
    With fail_open_on_error=True: unexpected exceptions → PRODUCTION.
    """

    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock(spec=QualityGateEngine)
        engine.config = {}
        engine.repository = MagicMock()
        return engine

    @pytest.fixture
    def gateway(self, mock_engine):
        return QualityGateGateway(mock_engine)

    @pytest.mark.asyncio
    async def test_validate_oserror_rejected(self, gateway, mock_engine):
        """OSError in pipeline returns rejected, not promoted."""
        def raise_os(*args, **kwargs):
            raise OSError("disk full")

        mock_engine.run_pipeline.side_effect = raise_os

        imp, passed = await gateway.validate("session-1", [], [])
        assert not passed
        assert imp.status == ImprovementStatus.REJECTED

    @pytest.mark.asyncio
    async def test_validate_unexpected_error_rejected_default(self, gateway, mock_engine):
        """Unexpected Exception with default config rejects."""
        def raise_err(*args, **kwargs):
            raise ValueError("pipeline bug")

        mock_engine.run_pipeline.side_effect = raise_err

        imp, passed = await gateway.validate("session-2", [], [])
        assert not passed
        assert imp.status == ImprovementStatus.REJECTED

    @pytest.mark.asyncio
    async def test_validate_fail_open_promotes(self, mock_engine):
        """With fail_open_on_error=True, unexpected errors promote to PRODUCTION."""
        mock_engine.config = {"gateway": {"fail_open_on_error": True}}
        mock_engine.repository = MagicMock()

        def raise_err(*args, **kwargs):
            raise ValueError("pipeline bug")

        mock_engine.run_pipeline.side_effect = raise_err

        gateway = QualityGateGateway(mock_engine)
        imp, passed = await gateway.validate("session-3", [], [])
        assert passed
        assert imp.status == ImprovementStatus.PRODUCTION

    @pytest.mark.asyncio
    async def test_validate_normal_flow_passes(self, gateway, mock_engine):
        """Normal successful validation still works."""
        imp = Improvement(name="ok", status=ImprovementStatus.PRODUCTION)
        mock_engine.run_pipeline.return_value = imp

        result, passed = await gateway.validate("session-4", [], [])
        assert passed
        assert result.status == ImprovementStatus.PRODUCTION