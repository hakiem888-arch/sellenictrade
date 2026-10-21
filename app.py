import copy
import os
import re
import sys
import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
import pandas as pd

try:
    import streamlit as st
except ImportError:  # Offline test fallback when streamlit is not installed
    class _DummySessionState(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as e:
                raise AttributeError(name) from e

        def __setattr__(self, name, value):
            self[name] = value

    class _DummyContext:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def __getattr__(self, name):
            return getattr(st, name)

    class _DummyStreamlit:
        def __init__(self):
            self.session_state = _DummySessionState()
            self.secrets = {}
            self.sidebar = self
            self.rendered_elements = []

        def set_page_config(self, **kwargs):
            self.rendered_elements.append(("set_page_config", kwargs))

        def title(self, text, **kwargs):
            self.rendered_elements.append(("title", str(text)))

        def header(self, text, **kwargs):
            self.rendered_elements.append(("header", str(text)))

        def subheader(self, text, **kwargs):
            self.rendered_elements.append(("subheader", str(text)))

        def caption(self, text, **kwargs):
            self.rendered_elements.append(("caption", str(text)))

        def markdown(self, text, **kwargs):
            self.rendered_elements.append(("markdown", str(text)))

        def info(self, text, **kwargs):
            self.rendered_elements.append(("info", str(text)))

        def warning(self, text, **kwargs):
            self.rendered_elements.append(("warning", str(text)))

        def error(self, text, **kwargs):
            self.rendered_elements.append(("error", str(text)))

        def success(self, text, **kwargs):
            self.rendered_elements.append(("success", str(text)))

        def divider(self, **kwargs):
            pass

        def metric(self, label, value, delta=None, **kwargs):
            self.rendered_elements.append(("metric", str(label), str(value), delta))

        def dataframe(self, data, **kwargs):
            self.rendered_elements.append(("dataframe", len(data) if hasattr(data, "__len__") else 0))

        def table(self, data, **kwargs):
            self.rendered_elements.append(("table", len(data) if hasattr(data, "__len__") else 0))

        def json(self, data, **kwargs):
            self.rendered_elements.append(("json", data))

        def code(self, body, **kwargs):
            self.rendered_elements.append(("code", str(body)[:120]))

        def plotly_chart(self, fig, **kwargs):
            self.rendered_elements.append(("plotly_chart", type(fig).__name__))

        def columns(self, spec, **kwargs):
            n = spec if isinstance(spec, int) else len(spec)
            return [_DummyContext() for _ in range(max(1, n))]

        def tabs(self, labels, **kwargs):
            return [_DummyContext() for _ in labels]

        def expander(self, label, **kwargs):
            return _DummyContext()

        def spinner(self, text="", **kwargs):
            return _DummyContext()

        def button(self, label, key=None, **kwargs):
            if key and key in self.session_state:
                return bool(self.session_state[key])
            return False

        def checkbox(self, label, value=False, key=None, **kwargs):
            if key and key in self.session_state:
                return bool(self.session_state[key])
            if key:
                self.session_state[key] = value
            return value

        def slider(self, label, min_value=0, max_value=100, value=None, step=None, key=None, **kwargs):
            val = self.session_state.get(key, value if value is not None else min_value) if key else (value if value is not None else min_value)
            if key:
                self.session_state[key] = val
            return val

        def selectbox(self, label, options, index=0, key=None, **kwargs):
            opts = list(options)
            default = opts[min(index, len(opts) - 1)] if opts else None
            val = self.session_state.get(key, default) if key else default
            if key:
                self.session_state[key] = val
            return val

        def multiselect(self, label, options, default=None, key=None, **kwargs):
            d = list(default) if default is not None else []
            val = self.session_state.get(key, d) if key else d
            if key:
                self.session_state[key] = val
            return val

        def radio(self, label, options, index=0, key=None, **kwargs):
            opts = list(options)
            default = opts[min(index, len(opts) - 1)] if opts else None
            val = self.session_state.get(key, default) if key else default
            if key:
                self.session_state[key] = val
            return val

        def text_input(self, label, value="", key=None, **kwargs):
            val = self.session_state.get(key, value) if key else value
            if key:
                self.session_state[key] = val
            return val

        def number_input(self, label, min_value=None, max_value=None, value=0, step=None, key=None, **kwargs):
            val = self.session_state.get(key, value) if key else value
            if key:
                self.session_state[key] = val
            return val

        def rerun(self):
            self.rendered_elements.append(("rerun", True))

    st = _DummyStreamlit()

try:
    import plotly.graph_objects as go
except ImportError:  # Offline test fallback when plotly is not installed
    class _DummyFigure:
        def __init__(self, *args, **kwargs):
            self.traces = []
            self.hrects = []
            self.layout = {}

        def add_trace(self, trace):
            self.traces.append(trace)

        def add_hrect(self, **kwargs):
            self.hrects.append(kwargs)

        def update_layout(self, **kwargs):
            self.layout.update(kwargs)

    class _DummyPlotlyGO:
        Figure = _DummyFigure

        @staticmethod
        def Candlestick(**kwargs):
            return {"type": "Candlestick", **kwargs}

        @staticmethod
        def Scatter(**kwargs):
            return {"type": "Scatter", **kwargs}

    go = _DummyPlotlyGO()

try:
    from groq import Groq
except ImportError:  # Offline test fallback when groq is not installed
    Groq = None


def _is_streamlit_runtime():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False

# -------------------------------------------------------------
# Modular Domain & Config Integration (PH02-01 / F34, F35)
# -------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_src_dir = os.path.join(_here, "src")
if os.path.isdir(_src_dir) and _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
if _here not in sys.path:
    sys.path.insert(0, _here)

try:
    import src.ai_consensus as _pkg_root
    import src.ai_consensus.config as _cfg_mod
    import src.ai_consensus.domain as _dom_mod
    import src.ai_consensus.storage as _stg_mod
    import src.ai_consensus.services as _svc_mod
    import src.ai_consensus.market as _mkt_mod
    import src.ai_consensus.features as _feat_mod
    import src.ai_consensus.smc as _smc_mod
    import src.ai_consensus.scanner as _scan_mod
    import src.ai_consensus.agents as _agt_mod
    import src.ai_consensus.providers as _prov_mod
    import src.ai_consensus.risk as _risk_mod
    import src.ai_consensus.paper as _paper_mod
    import src.ai_consensus.monitor as _mon_mod
except ImportError:
    try:
        import ai_consensus as _pkg_root
        import ai_consensus.config as _cfg_mod
        import ai_consensus.domain as _dom_mod
        import ai_consensus.storage as _stg_mod
        import ai_consensus.services as _svc_mod
        import ai_consensus.market as _mkt_mod
        import ai_consensus.features as _feat_mod
        import ai_consensus.smc as _smc_mod
        import ai_consensus.scanner as _scan_mod
        import ai_consensus.agents as _agt_mod
        import ai_consensus.providers as _prov_mod
        import ai_consensus.risk as _risk_mod
        import ai_consensus.paper as _paper_mod
        import ai_consensus.monitor as _mon_mod
    except ImportError:
        _cfg_mod = None
        _dom_mod = None
        _stg_mod = None
        _svc_mod = None
        _mkt_mod = None
        _feat_mod = None
        _smc_mod = None
        _scan_mod = None
        _agt_mod = None
        _prov_mod = None
        _risk_mod = None
        _paper_mod = None
        _mon_mod = None

import importlib.util as _ilu
for _mname, _mvar in (("paper", "_paper_mod"), ("monitor", "_mon_mod")):
    if globals().get(_mvar) is None:
        for _cand_dir in (
            os.path.join(_src_dir, "ai_consensus"),
            os.path.abspath(os.path.join(_here, "..", "..", "..", "..", "src", "ai_consensus")),
        ):
            _mpath = os.path.join(_cand_dir, f"{_mname}.py")
            if os.path.exists(_mpath):
                _mspec = _ilu.spec_from_file_location(f"src.ai_consensus.{_mname}", _mpath)
                if _mspec and _mspec.loader:
                    _mobj = _ilu.module_from_spec(_mspec)
                    sys.modules[f"src.ai_consensus.{_mname}"] = _mobj
                    sys.modules[f"ai_consensus.{_mname}"] = _mobj
                    _mspec.loader.exec_module(_mobj)
                    globals()[_mvar] = _mobj
                    break

_EMBEDDED_AI_CONSENSUS_B64 = "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"
_EMBEDDED_AI_CONSENSUS_HASH = "4d70b01335f86e65"

if any(m is None for m in (_cfg_mod, _dom_mod, _mkt_mod, _feat_mod, _smc_mod, _scan_mod, _agt_mod, _prov_mod, _risk_mod, _paper_mod, _stg_mod, _svc_mod, _mon_mod)) or not hasattr(_stg_mod, 'compute_dual_performance_metrics') or not hasattr(_mon_mod, 'get_durable_monitor_runtime_status') or (getattr(_cfg_mod, '__file__', '').startswith('<embedded:') and getattr(_pkg_root, '_EMBEDDED_HASH', None) != _EMBEDDED_AI_CONSENSUS_HASH):
    try:
        import zlib as _zlib, base64 as _b64, types as _types
        _emb_map = json.loads(_zlib.decompress(_b64.b64decode(_EMBEDDED_AI_CONSENSUS_B64.encode("ascii"))).decode("utf-8"))
        _src_root = sys.modules.get("src") or _types.ModuleType("src")
        _src_root.__path__ = []
        sys.modules["src"] = _src_root
        _pkg_mod = _types.ModuleType("src.ai_consensus")
        _pkg_mod.__path__ = []
        _pkg_mod.__package__ = "src.ai_consensus"
        sys.modules["src.ai_consensus"] = _pkg_mod
        sys.modules["ai_consensus"] = _pkg_mod
        setattr(_src_root, "ai_consensus", _pkg_mod)
        for _m_name in ['config', 'domain', 'market', 'features', 'smc', 'scanner', 'agents', 'providers', 'risk', 'paper', 'storage', 'services', 'monitor']:
            _full_s = f"src.ai_consensus.{_m_name}"
            _full_a = f"ai_consensus.{_m_name}"
            _m_inst = _types.ModuleType(_full_s)
            _m_inst.__file__ = f"<embedded:{_full_s}>"
            _m_inst.__package__ = "src.ai_consensus"
            sys.modules[_full_s] = _m_inst
            sys.modules[_full_a] = _m_inst
            exec(compile(_emb_map[_m_name], _m_inst.__file__, "exec"), _m_inst.__dict__)
            setattr(_pkg_mod, _m_name, _m_inst)
        _pkg_mod._EMBEDDED_HASH = _EMBEDDED_AI_CONSENSUS_HASH
        _pkg_root = _pkg_mod
        _cfg_mod = sys.modules["src.ai_consensus.config"]
        _dom_mod = sys.modules["src.ai_consensus.domain"]
        _mkt_mod = sys.modules["src.ai_consensus.market"]
        _feat_mod = sys.modules["src.ai_consensus.features"]
        _smc_mod = sys.modules["src.ai_consensus.smc"]
        _scan_mod = sys.modules["src.ai_consensus.scanner"]
        _agt_mod = sys.modules["src.ai_consensus.agents"]
        _prov_mod = sys.modules["src.ai_consensus.providers"]
        _risk_mod = sys.modules["src.ai_consensus.risk"]
        _paper_mod = sys.modules["src.ai_consensus.paper"]
        _stg_mod = sys.modules["src.ai_consensus.storage"]
        _svc_mod = sys.modules["src.ai_consensus.services"]
        _mon_mod = sys.modules["src.ai_consensus.monitor"]
    except Exception:
        pass


import types
if "src" not in sys.modules:
    _src_pkg = types.ModuleType("src")
    if os.path.isdir(_src_dir):
        _src_pkg.__path__ = [_src_dir]
    sys.modules["src"] = _src_pkg
if _cfg_mod is not None:
    sys.modules["src.ai_consensus.config"] = _cfg_mod
    sys.modules["ai_consensus.config"] = _cfg_mod
if _dom_mod is not None:
    sys.modules["src.ai_consensus.domain"] = _dom_mod
    sys.modules["ai_consensus.domain"] = _dom_mod
if _stg_mod is not None:
    sys.modules["src.ai_consensus.storage"] = _stg_mod
    sys.modules["ai_consensus.storage"] = _stg_mod
if _svc_mod is not None:
    sys.modules["src.ai_consensus.services"] = _svc_mod
    sys.modules["ai_consensus.services"] = _svc_mod
if _mkt_mod is not None:
    sys.modules["src.ai_consensus.market"] = _mkt_mod
    sys.modules["ai_consensus.market"] = _mkt_mod
if _feat_mod is not None:
    sys.modules["src.ai_consensus.features"] = _feat_mod
    sys.modules["ai_consensus.features"] = _feat_mod
if _smc_mod is not None:
    sys.modules["src.ai_consensus.smc"] = _smc_mod
    sys.modules["ai_consensus.smc"] = _smc_mod
if _scan_mod is not None:
    sys.modules["src.ai_consensus.scanner"] = _scan_mod
    sys.modules["ai_consensus.scanner"] = _scan_mod
if _agt_mod is not None:
    sys.modules["src.ai_consensus.agents"] = _agt_mod
    sys.modules["ai_consensus.agents"] = _agt_mod
if _prov_mod is not None:
    sys.modules["src.ai_consensus.providers"] = _prov_mod
    sys.modules["ai_consensus.providers"] = _prov_mod
if _risk_mod is not None:
    sys.modules["src.ai_consensus.risk"] = _risk_mod
    sys.modules["ai_consensus.risk"] = _risk_mod

if _dom_mod is not None:
    DecisionAction = _dom_mod.DecisionAction
    DirectionalBias = _dom_mod.DirectionalBias
    MarketRegime = _dom_mod.MarketRegime
    TimingStatus = _dom_mod.TimingStatus
    AgentStatus = _dom_mod.AgentStatus
    DataStatus = _dom_mod.DataStatus
    TradingMode = _dom_mod.TradingMode
    TradePlan = _dom_mod.TradePlan
    GateReport = _dom_mod.GateReport
    ModeDecisionData = _dom_mod.ModeDecisionData
    CORE_MARKET_TIMEFRAMES = _dom_mod.CORE_MARKET_TIMEFRAMES
    HIGHER_MARKET_TIMEFRAMES = _dom_mod.HIGHER_MARKET_TIMEFRAMES
    ALL_MARKET_TIMEFRAMES = _dom_mod.ALL_MARKET_TIMEFRAMES
    is_timeframe_warmed_up = _dom_mod.is_timeframe_warmed_up
if _cfg_mod is not None:
    ModelTierConfig = _cfg_mod.ModelTierConfig
    RiskConfig = _cfg_mod.RiskConfig
    StorageConfig = _cfg_mod.StorageConfig
    AppConfig = _cfg_mod.AppConfig
    mask_secret = _cfg_mod.mask_secret
    redact_secrets = _cfg_mod.redact_secrets
    get_groq_keys = _cfg_mod.get_groq_keys
    get_gemini_keys = _cfg_mod.get_gemini_keys
    is_gemini_enabled = _cfg_mod.is_gemini_enabled
    DEFAULT_GEMINI_ANALYST_MODEL = _cfg_mod.DEFAULT_GEMINI_ANALYST_MODEL
    DEFAULT_GEMINI_DEBATE_MODEL = _cfg_mod.DEFAULT_GEMINI_DEBATE_MODEL
    DEFAULT_GEMINI_RESEARCH_MODEL = _cfg_mod.DEFAULT_GEMINI_RESEARCH_MODEL
    DEFAULT_GEMINI_JUDGE_MODEL = _cfg_mod.DEFAULT_GEMINI_JUDGE_MODEL
    STRATEGY_VERSION = _cfg_mod.STRATEGY_VERSION
    PROMPT_VERSION = _cfg_mod.PROMPT_VERSION
    SCHEMA_VERSION_AI = _cfg_mod.SCHEMA_VERSION_AI
    DEPLOYMENT_MODE = _cfg_mod.DEPLOYMENT_MODE
    compute_model_config_hash = _cfg_mod.compute_model_config_hash
    validate_deployment_security = _cfg_mod.validate_deployment_security
if _stg_mod is not None:
    connect_db = _stg_mod.connect_db
    run_migrations = _stg_mod.run_migrations
    backup_database = _stg_mod.backup_database
    restore_database = _stg_mod.restore_database
    verify_database_integrity = _stg_mod.verify_database_integrity
    compute_consensus_score = _stg_mod.compute_consensus_score
    get_signal_provenance = _stg_mod.get_signal_provenance
    update_pattern_event_workflow = _stg_mod.update_pattern_event_workflow
    get_pattern_event_workflow = _stg_mod.get_pattern_event_workflow
    CURRENT_SCHEMA_VERSION = _stg_mod.CURRENT_SCHEMA_VERSION
    QuotaScopeRecord = _stg_mod.QuotaScopeRecord
    BudgetReservationRecord = _stg_mod.BudgetReservationRecord
    DurableBudgetManager = _stg_mod.DurableBudgetManager
    GLOBAL_DURABLE_BUDGET_MANAGER = _stg_mod.GLOBAL_DURABLE_BUDGET_MANAGER
    DAGCheckpointRecord = _stg_mod.DAGCheckpointRecord
    save_dag_checkpoint = _stg_mod.save_dag_checkpoint
    load_dag_checkpoints = _stg_mod.load_dag_checkpoints
    clear_dag_checkpoints = _stg_mod.clear_dag_checkpoints
if _svc_mod is not None:
    sanitize_ui_error = _svc_mod.sanitize_ui_error
    ensure_session_state = _svc_mod.ensure_session_state
    store_analysis_snapshot = _svc_mod.store_analysis_snapshot
    get_rendered_analysis_snapshot = _svc_mod.get_rendered_analysis_snapshot
    run_and_store_analysis_service = _svc_mod.run_and_store_analysis_service
    analyze_candidates_batch_service = _svc_mod.analyze_candidates_batch_service
    send_telegram_notification = _svc_mod.send_telegram_notification
    save_rendered_analysis_service = _svc_mod.save_rendered_analysis_service
    update_paper_trades_service = _svc_mod.update_paper_trades_service
    compute_performance_service = _svc_mod.compute_performance_service
    reset_trading_state_service = _svc_mod.reset_trading_state_service
if _mkt_mod is not None:
    INTERVAL_SECONDS = _mkt_mod.INTERVAL_SECONDS
    DEFAULT_CACHE_TTL_SECONDS = _mkt_mod.DEFAULT_CACHE_TTL_SECONDS
    BarWatermark = _mkt_mod.BarWatermark
    LiveBarQuote = _mkt_mod.LiveBarQuote
    ValidationReport = _mkt_mod.ValidationReport
    TimeframeSnapshot = _mkt_mod.TimeframeSnapshot
    MarketSnapshotCache = _mkt_mod.MarketSnapshotCache
    GLOBAL_MARKET_CACHE = _mkt_mod.GLOBAL_MARKET_CACHE
    normalize_as_of_ms = _mkt_mod.normalize_as_of_ms
    ms_to_iso = _mkt_mod.ms_to_iso
    annotate_bar_watermarks = _mkt_mod.annotate_bar_watermarks
    extract_closed_df = _mkt_mod.extract_closed_df
    validate_ohlcv_dataframe = _mkt_mod.validate_ohlcv_dataframe
    build_timeframe_snapshot = _mkt_mod.build_timeframe_snapshot
    filter_bars_since = _mkt_mod.filter_bars_since
    get_monitor_runtime_status = _mkt_mod.get_monitor_runtime_status
if _feat_mod is not None:
    INDICATOR_VERSION = _feat_mod.INDICATOR_VERSION
    DEFAULT_KLINES_LIMIT = _feat_mod.DEFAULT_KLINES_LIMIT
    MIN_BARS_CORE_INDICATORS = _feat_mod.MIN_BARS_CORE_INDICATORS
    MIN_BARS_EMA50 = _feat_mod.MIN_BARS_EMA50
    MIN_BARS_EMA200 = _feat_mod.MIN_BARS_EMA200
    RECOMMENDED_BARS_EMA200 = _feat_mod.RECOMMENDED_BARS_EMA200
    WARMUP_REQUIREMENTS = _feat_mod.WARMUP_REQUIREMENTS
    WarmupAndQualityReport = _feat_mod.WarmupAndQualityReport
    evaluate_warmup_and_quality = _feat_mod.evaluate_warmup_and_quality
    compute_indicators_frame = _feat_mod.compute_indicators_frame
    safe_candle_info = _feat_mod.safe_candle_info
    safe_market_regime = _feat_mod.safe_market_regime
    extract_timeframe_features = _feat_mod.extract_timeframe_features
if _smc_mod is not None:
    SMC_ENGINE_VERSION = _smc_mod.SMC_ENGINE_VERSION
    compute_zone_boundary_distance_pct = _smc_mod.compute_zone_boundary_distance_pct
    compute_zone_overlap = _smc_mod.compute_zone_overlap
    find_pivots = _smc_mod.find_pivots
    detect_structure = _smc_mod.detect_structure
    detect_liquidity = _smc_mod.detect_liquidity
    detect_fvgs = _smc_mod.detect_fvgs
    detect_order_blocks = _smc_mod.detect_order_blocks
    compute_zone_quality = _smc_mod.compute_zone_quality
    build_entry_context = _smc_mod.build_entry_context
if _scan_mod is not None:
    SCANNER_VERSION = _scan_mod.SCANNER_VERSION
    BinanceRateLimitError = _scan_mod.BinanceRateLimitError
    BinanceRequestBudget = _scan_mod.BinanceRequestBudget
    GLOBAL_REQUEST_BUDGET = _scan_mod.GLOBAL_REQUEST_BUDGET
    ScanDiagnosticsReport = _scan_mod.ScanDiagnosticsReport
    scanner_score_bidirectional = _scan_mod.scanner_score_bidirectional
    filter_liquid_universe = _scan_mod.filter_liquid_universe
    scan_markets_service = _scan_mod.scan_markets_service
    extract_retry_after_seconds = _scan_mod.extract_retry_after_seconds
    is_http_429_error = _scan_mod.is_http_429_error
if _agt_mod is not None:
    AGENTS_ENGINE_VERSION = _agt_mod.AGENTS_ENGINE_VERSION
    AI_BASE_SCHEMA = _agt_mod.AI_BASE_SCHEMA
    AI_JUDGE_SCHEMA = _agt_mod.AI_JUDGE_SCHEMA
    NEUTRAL_ANALYSTS = _agt_mod.NEUTRAL_ANALYSTS
    is_judge_role = _agt_mod.is_judge_role
    get_schema_for_role = _agt_mod.get_schema_for_role
    detect_prompt_injection = _agt_mod.detect_prompt_injection
    sanitize_untrusted_text = _agt_mod.sanitize_untrusted_text
    sanitize_agent_output_for_downstream = _agt_mod.sanitize_agent_output_for_downstream
    build_bull_instructions = _agt_mod.build_bull_instructions
    build_bear_instructions = _agt_mod.build_bear_instructions
    build_agent_prompt = _agt_mod.build_agent_prompt
    format_debate_transcript = _agt_mod.format_debate_transcript
    run_debate_rounds = _agt_mod.run_debate_rounds
    compute_directional_consensus = _agt_mod.compute_directional_consensus
    classify_provider_error = _agt_mod.classify_provider_error
    WorkflowStageStatus = _agt_mod.WorkflowStageStatus
    build_workflow_stage_status = _agt_mod.build_workflow_stage_status
    DAGValidationError = _agt_mod.DAGValidationError
    DAGNodeSpec = _agt_mod.DAGNodeSpec
    validate_and_toposort_dag = _agt_mod.validate_and_toposort_dag
    ANALYST_NODE_IDS = _agt_mod.ANALYST_NODE_IDS
    RISK_NODE_IDS = _agt_mod.RISK_NODE_IDS
    build_standard_council_dag = _agt_mod.build_standard_council_dag
    AnalysisDAGOrchestrator = _agt_mod.AnalysisDAGOrchestrator
if _prov_mod is not None:
    PROVIDERS_ENGINE_VERSION = _prov_mod.PROVIDERS_ENGINE_VERSION
    ProviderError = _prov_mod.ProviderError
    ProviderAuthError = _prov_mod.ProviderAuthError
    ProviderModelUnsupportedError = _prov_mod.ProviderModelUnsupportedError
    ProviderRateLimitError = _prov_mod.ProviderRateLimitError
    ProviderDailyQuotaError = _prov_mod.ProviderDailyQuotaError
    ProviderNetworkTimeoutError = _prov_mod.ProviderNetworkTimeoutError
    ProviderMalformedOutputError = _prov_mod.ProviderMalformedOutputError
    ModelCapability = _prov_mod.ModelCapability
    DEFAULT_CAPABILITY_REGISTRY = _prov_mod.DEFAULT_CAPABILITY_REGISTRY
    register_model_capability = _prov_mod.register_model_capability
    get_model_capability = _prov_mod.get_model_capability
    parse_duration_header_seconds = _prov_mod.parse_duration_header_seconds
    parse_rate_limit_headers = _prov_mod.parse_rate_limit_headers
    coerce_to_provider_error = _prov_mod.coerce_to_provider_error
    build_provider_request_payload = _prov_mod.build_provider_request_payload
    verify_prompt_schema_alignment = _prov_mod.verify_prompt_schema_alignment
    BaseProviderAdapter = _prov_mod.BaseProviderAdapter
    GroqProviderAdapter = _prov_mod.GroqProviderAdapter
    OptionalGeminiProviderAdapter = _prov_mod.OptionalGeminiProviderAdapter
    BudgetContext = _prov_mod.BudgetContext
    create_budget_context = _prov_mod.create_budget_context
    preflight_reserve_judge_budget = _prov_mod.preflight_reserve_judge_budget
    GEMINI_DEFAULT_TIER_MODELS = _prov_mod.GEMINI_DEFAULT_TIER_MODELS
    should_fallback_to_secondary_provider = _prov_mod.should_fallback_to_secondary_provider
    build_gemini_http_payload = _prov_mod.build_gemini_http_payload
if _risk_mod is not None:
    RISK_ENGINE_VERSION = _risk_mod.RISK_ENGINE_VERSION
    AccountRiskConfig = _risk_mod.AccountRiskConfig
    InstrumentSemantics = _risk_mod.InstrumentSemantics
    PositionSizingResult = _risk_mod.PositionSizingResult
    PaperPortfolioLedger = _risk_mod.PaperPortfolioLedger
    GLOBAL_RISK_LEDGER = _risk_mod.GLOBAL_RISK_LEDGER
    resolve_instrument_semantics = _risk_mod.resolve_instrument_semantics
    compute_position_sizing = _risk_mod.compute_position_sizing
    build_trade_plan_and_sizing = _risk_mod.build_trade_plan_and_sizing
if _paper_mod is not None:
    sys.modules["src.ai_consensus.paper"] = _paper_mod
    sys.modules["ai_consensus.paper"] = _paper_mod
    PAPER_ENGINE_VERSION = _paper_mod.PAPER_ENGINE_VERSION
    REALISTIC_ACCOUNTING_MODE = _paper_mod.REALISTIC_ACCOUNTING_MODE
    REALISTIC_ACCOUNTING_QUALITY = _paper_mod.REALISTIC_ACCOUNTING_QUALITY
    PaperPositionState = _paper_mod.PaperPositionState
    IntrabarTieBreaker = _paper_mod.IntrabarTieBreaker
    RealizedTradeAccounting = _paper_mod.RealizedTradeAccounting
    validate_position_transition = _paper_mod.validate_position_transition
    compute_realized_trade_accounting = _paper_mod.compute_realized_trade_accounting
    resolve_intrabar_conflict = _paper_mod.resolve_intrabar_conflict
    evaluate_position_on_bars = _paper_mod.evaluate_position_on_bars
    compute_expectancy_and_drawdown_analytics = _paper_mod.compute_expectancy_and_drawdown_analytics
    compute_confidence_calibration_report = _paper_mod.compute_confidence_calibration_report
    run_walk_forward_replay_backtest = _paper_mod.run_walk_forward_replay_backtest
    run_walk_forward_folds = _paper_mod.run_walk_forward_folds
if _stg_mod is not None:
    reconcile_paper_positions = _stg_mod.reconcile_paper_positions
    compute_dual_performance_metrics = _stg_mod.compute_dual_performance_metrics
    record_worker_heartbeat = _stg_mod.record_worker_heartbeat
    get_worker_heartbeat_state = _stg_mod.get_worker_heartbeat_state
    clear_worker_heartbeats = _stg_mod.clear_worker_heartbeats
if _mon_mod is not None:
    sys.modules["src.ai_consensus.monitor"] = _mon_mod
    sys.modules["ai_consensus.monitor"] = _mon_mod
    MONITOR_ENGINE_VERSION = _mon_mod.MONITOR_ENGINE_VERSION
    MonitorCycleReport = _mon_mod.MonitorCycleReport
    get_durable_monitor_runtime_status = _mon_mod.get_durable_monitor_runtime_status
    run_monitor_cycle = _mon_mod.run_monitor_cycle
    BackgroundMonitorWorker = _mon_mod.BackgroundMonitorWorker
    parse_cli_args = _mon_mod.parse_cli_args


# ============================================================
# AI CONSENSUS TRADING V5.5
# TradingAgents-style multi-agent crypto research system
# ============================================================

def _configure_page():
    st.set_page_config(
        page_title="AI Consensus Trading V5.6",
        page_icon="🧠",
        layout="wide",
    )

BINANCE = "https://data-api.binance.vision"
DB_FILE = "trading_v5.db"

# -------------------------
# Secrets
# -------------------------
def secret(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")

def _parse_secret_list(value):
    """Accept a single string, comma/newline separated keys, or a Streamlit list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re.split(r"[\s,]+", str(value))
    return [str(x).strip() for x in raw_items if str(x).strip()]


_GROQ_KEYS = []
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEY")))
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEYS")))
for _i in range(1, 21):
    _GROQ_KEYS.extend(_parse_secret_list(secret(f"GROQ_API_KEY_{_i}")))

GROQ_KEYS = list(dict.fromkeys(k for k in _GROQ_KEYS if k))
GROQ_KEY = GROQ_KEYS[0] if GROQ_KEYS else ""

_GEMINI_KEYS = []
_GEMINI_KEYS.extend(_parse_secret_list(secret("GEMINI_API_KEY")))
_GEMINI_KEYS.extend(_parse_secret_list(secret("GEMINI_API_KEYS")))
_GEMINI_KEYS.extend(_parse_secret_list(secret("GOOGLE_API_KEY")))
for _i in range(1, 21):
    _GEMINI_KEYS.extend(_parse_secret_list(secret(f"GEMINI_API_KEY_{_i}")))
GEMINI_KEYS = list(dict.fromkeys(k for k in _GEMINI_KEYS if k))
GEMINI_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else ""
GEMINI_ENABLED = str(secret("GEMINI_ENABLED") or secret("ENABLE_GEMINI_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}

TG_TOKEN = secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = secret("TELEGRAM_CHAT_ID")

# -------------------------
# Database
# -------------------------
def db():
    if _stg_mod is not None:
        return _stg_mod.connect_db(DB_FILE)
    con = sqlite3.connect(DB_FILE, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        scanner_score REAL,
        final_decision TEXT NOT NULL,
        confidence REAL,
        entry REAL,
        stop_loss REAL,
        take_profit REAL,
        risk_reward REAL,
        market_regime TEXT,
        score_consensus REAL,
        reasoning TEXT,
        closed_at TEXT,
        result TEXT,
        r_multiple REAL,
        exit_price REAL
    );

    CREATE TABLE IF NOT EXISTS agent_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        agent TEXT NOT NULL,
        decision TEXT NOT NULL,
        confidence REAL,
        reasoning TEXT,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    );

    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT,
        memory_type TEXT NOT NULL,
        content TEXT NOT NULL,
        signal_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS pattern_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        mode TEXT NOT NULL,
        state TEXT NOT NULL,
        direction TEXT NOT NULL,
        score REAL NOT NULL,
        fingerprint TEXT NOT NULL,
        notified INTEGER DEFAULT 0
    );
    """)
    con.commit()
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    for col, ddl in [
        ("smc_score", "ALTER TABLE signals ADD COLUMN smc_score REAL"),
        ("entry_status", "ALTER TABLE signals ADD COLUMN entry_status TEXT"),
        ("entry_zone_low", "ALTER TABLE signals ADD COLUMN entry_zone_low REAL"),
        ("entry_zone_high", "ALTER TABLE signals ADD COLUMN entry_zone_high REAL"),
        ("smc_bias", "ALTER TABLE signals ADD COLUMN smc_bias TEXT"),
        ("analysis_id", "ALTER TABLE signals ADD COLUMN analysis_id TEXT"),
        ("idempotency_key", "ALTER TABLE signals ADD COLUMN idempotency_key TEXT"),
        ("raw_model_decision", "ALTER TABLE signals ADD COLUMN raw_model_decision TEXT"),
        ("veto_reasons", "ALTER TABLE signals ADD COLUMN veto_reasons TEXT"),
        ("accounting_mode", "ALTER TABLE signals ADD COLUMN accounting_mode TEXT"),
    ]:
        if col not in existing_cols:
            con.execute(ddl)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_analysis_id "
        "ON signals(analysis_id) WHERE analysis_id IS NOT NULL"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_idempotency_key "
        "ON signals(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_events_fingerprint "
        "ON pattern_events(fingerprint)"
    )
    con.commit()
    con.close()
    if _stg_mod is not None:
        _stg_mod.run_migrations(DB_FILE)

# -------------------------
# Binance public API
# -------------------------
_BINANCE_FALLBACK_HOSTS = (
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)
_HTTP_SESSION_LOCAL = threading.local()


def _get_http_session():
    sess = getattr(_HTTP_SESSION_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "AI-Consensus-Trading-V5",
            "Accept": "application/json",
        })
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
        except Exception:
            pass
        _HTTP_SESSION_LOCAL.session = sess
    return sess


def bget(path, params=None):
    headers = {
        "User-Agent": "AI-Consensus-Trading-V5",
        "Accept": "application/json",
    }
    hosts = [BINANCE] + [h for h in _BINANCE_FALLBACK_HOSTS if h != BINANCE]
    use_session = getattr(requests.get, "__module__", "").startswith("requests")
    getter = _get_http_session().get if use_session else requests.get

    last_exc = None
    r = None
    for host in hosts:
        try:
            r = getter(
                host + path,
                params=params,
                timeout=12,
                headers=headers,
            )
            if r.status_code in (451, 403, 502, 503, 504) and host != hosts[-1]:
                continue
            break
        except Exception as exc:
            last_exc = exc
            if host == hosts[-1]:
                raise
            continue
    if r is None and last_exc is not None:
        raise last_exc
    if r.status_code in (429, 418):
        hdrs = getattr(r, "headers", {}) or {}
        ra_raw = hdrs.get("Retry-After") or hdrs.get("retry-after") or 30.0
        try:
            ra_sec = max(1.0, float(ra_raw))
        except (TypeError, ValueError):
            ra_sec = 30.0
        detail = (r.text or "")[:500].replace("\n", " ")
        if _scan_mod is not None:
            GLOBAL_REQUEST_BUDGET.record_rate_limit(ra_sec)
            raise BinanceRateLimitError(
                f"Binance HTTP {r.status_code} (Retry-After={ra_sec}s): {detail}",
                status_code=r.status_code,
                retry_after=ra_sec,
            )
        raise RuntimeError(f"Binance HTTP {r.status_code} (Retry-After={ra_sec}s): {detail}")
    if r.status_code != 200:
        detail = (r.text or "")[:500].replace("\n", " ")
        raise RuntimeError(f"Binance HTTP {r.status_code}: {detail}")
    return r.json()

def binance_health():
    try:
        bget("/api/v3/ping")
        return True, "Binance public data API ONLINE"
    except Exception as e:
        return False, str(e)

# -------------------------
# Indicators
# -------------------------
def ema(s, n):
    if _feat_mod is not None:
        return _feat_mod.ema(s, n)
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14, min_periods=None, flat_value=50.0):
    if _feat_mod is not None:
        return _feat_mod.rsi(s, n=n, min_periods=min_periods, flat_value=flat_value)
    d = s.diff()
    gain = d.where(d > 0, 0.0).fillna(0.0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.where(d < 0, 0.0)).fillna(0.0).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(flat_value)


def atr(df, n=14):
    if _feat_mod is not None:
        return _feat_mod.atr(df, n=n)
    tr = pd.concat([
        df.High - df.Low,
        (df.High - df.Close.shift()).abs(),
        (df.Low - df.Close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def macd(s):
    if _feat_mod is not None:
        return _feat_mod.macd(s)
    line = ema(s, 12) - ema(s, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def bollinger(s, n=20):
    if _feat_mod is not None:
        return _feat_mod.bollinger(s, n=n)
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std().fillna(0.0)
    return mid, mid + 2 * sd, mid - 2 * sd


def klines(symbol, interval="15m", limit=260, drop_unwarmed=True):
    x = bget(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )

    cols = [
        "time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "QuoteVolume", "Trades",
        "TakerBuyBase", "TakerBuyQuote", "ignore"
    ]

    d = pd.DataFrame(x, columns=cols)
    if len(d) == 0:
        return _feat_mod.compute_indicators_frame(d, interval=interval, drop_unwarmed=drop_unwarmed) if _feat_mod is not None else d

    numeric = [
        "Open", "High", "Low", "Close", "Volume",
        "QuoteVolume", "Trades", "TakerBuyBase", "TakerBuyQuote"
    ]
    for c in numeric:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    d.index = pd.to_datetime(d.time, unit="ms", utc=True)

    if _feat_mod is not None:
        return _feat_mod.compute_indicators_frame(d, interval=interval, drop_unwarmed=drop_unwarmed)

    d["EMA20"] = ema(d.Close, 20)
    d["EMA50"] = ema(d.Close, 50)
    d["EMA200"] = ema(d.Close, 200)
    d["RSI"] = rsi(d.Close)
    d["MACD"], d["MACD_SIGNAL"], d["MACD_HIST"] = macd(d.Close)
    d["ATR"] = atr(d)
    d["BB_MID"], d["BB_UP"], d["BB_LOW"] = bollinger(d.Close)
    d["VOL_RATIO"] = (d.Volume / d.Volume.rolling(20).mean().replace(0, pd.NA)).fillna(0.0)
    d["BUY_PRESSURE"] = (d.TakerBuyQuote / d.QuoteVolume.replace(0, pd.NA)).fillna(0.5)
    return d.dropna(subset=["Open", "High", "Low", "Close", "Volume", "EMA20", "RSI", "ATR", "BB_MID"])

def order_book(symbol, limit=20):
    x = bget("/api/v3/depth", {"symbol": symbol, "limit": limit})

    bid = sum(float(q) for _, q in x.get("bids", []))
    ask = sum(float(q) for _, q in x.get("asks", []))
    total = bid + ask

    imbalance = (bid - ask) / total if total else 0

    best_bid = float(x["bids"][0][0]) if x.get("bids") else 0
    best_ask = float(x["asks"][0][0]) if x.get("asks") else 0

    spread = (
        (best_ask - best_bid) / best_bid * 100
        if best_bid else 0
    )

    return {
        "imbalance": imbalance,
        "spread": spread,
        "bid_depth": bid,
        "ask_depth": ask,
    }

def candle_info(d):
    if _feat_mod is not None:
        return _feat_mod.safe_candle_info(d)
    if d is None or len(d) == 0:
        return {"name": "INSUFFICIENT_DATA", "body": 0.0, "upper_wick": 0.0, "lower_wick": 0.0}
    a = d.iloc[-1]
    p = d.iloc[-2] if len(d) >= 2 else a

    body = abs(a.Close - a.Open)
    rng = max(a.High - a.Low, 1e-12)

    upper = a.High - max(a.Open, a.Close)
    lower = min(a.Open, a.Close) - a.Low

    body_ratio = body / rng

    if body_ratio < 0.10:
        name = "DOJI"
    elif lower > body * 2 and upper < max(body, 1e-12):
        name = "HAMMER BULLISH"
    elif upper > body * 2 and lower < max(body, 1e-12):
        name = "SHOOTING STAR BEARISH"
    elif (
        a.Close > a.Open and
        p.Close < p.Open and
        a.Open <= p.Close and
        a.Close >= p.Open
    ):
        name = "BULLISH ENGULFING"
    elif (
        a.Close < a.Open and
        p.Close > p.Open and
        a.Open >= p.Close and
        a.Close <= p.Open
    ):
        name = "BEARISH ENGULFING"
    elif a.Close > a.Open:
        name = "BULLISH"
    elif a.Close < a.Open:
        name = "BEARISH"
    else:
        name = "NETRAL"

    return {
        "name": name,
        "body": body_ratio,
        "upper_wick": upper / rng,
        "lower_wick": lower / rng,
    }


# -------------------------
# Smart Money / Price Structure Engine V5.5
# -------------------------
def _closed_df(df, as_of=None, interval="15m"):
    """Idempotently extract only fully closed candles without double-dropping bars (PH03-01 / F08 / T06)."""
    if _mkt_mod is not None:
        return _mkt_mod.extract_closed_df(df, as_of=as_of, interval=interval)
    if df is None or len(df) == 0:
        out = pd.DataFrame() if df is None else df.copy()
        out.attrs["_is_closed_df"] = True
        return out
    if df.attrs.get("_is_closed_df", False):
        out = df.copy()
        out.attrs["_is_closed_df"] = True
        return out
    out = df.iloc[:-1].copy() if len(df) > 8 else df.copy()
    out.attrs["_is_closed_df"] = True
    return out


def kline_snapshot(symbol, interval="15m", limit=180, as_of=None, use_cache=False, cache=None, ttl_seconds=None):
    """Build or retrieve a cached immutable TimeframeSnapshot separating closed_df from live_bar (PH03-01)."""
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    if use_cache and active_cache is not None:
        cached = active_cache.get_kline_snapshot(symbol, interval, limit, now_ms=as_of)
        if cached is not None:
            return cached
    raw_df = klines(symbol, interval=interval, limit=limit)
    snap = build_timeframe_snapshot(
        raw_df,
        symbol=symbol,
        interval=interval,
        as_of=as_of,
        ttl_seconds=ttl_seconds,
        limit=limit,
    )
    if use_cache and active_cache is not None:
        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms, ttl_seconds=ttl_seconds)
    return snap

def _pct_distance(a, b):
    return abs(a - b) / max(abs(b), 1e-12) * 100.0

def _find_pivots(df, left=2, right=2, lookback=100, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.find_pivots(df, left=left, right=right, lookback=lookback, as_of=as_of, interval=interval)
    return [], []


def _detect_structure(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_structure(df, as_of=as_of, interval=interval)
    return {
        "bias": "RANGE",
        "bos": "NONE",
        "choch": "NONE",
        "last_swing_high": 0.0,
        "last_swing_low": 0.0,
        "previous_swing_high": 0.0,
        "previous_swing_low": 0.0,
    }


def _detect_liquidity(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_liquidity(df, as_of=as_of, interval=interval)
    return {"sweep_high": False, "sweep_low": False, "equal_high": None, "equal_low": None}


def _detect_fvgs(df, max_age=80, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_fvgs(df, max_age=max_age, as_of=as_of, interval=interval)
    return []


def _detect_order_blocks(df, max_age=80, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_order_blocks(df, max_age=max_age, as_of=as_of, interval=interval)
    return []


def _zone_quality(zone, fvg=None, structure=None, liquidity=None):
    if _smc_mod is not None:
        return _smc_mod.compute_zone_quality(zone, fvg=fvg, structure=structure, liquidity=liquidity)
    return 0


def _build_entry_context(df, structure, liquidity, obs, fvgs, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.build_entry_context(df, structure, liquidity, obs, fvgs, as_of=as_of, interval=interval)
    return {}


def smc_analysis(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.smc_analysis(df, as_of=as_of, interval=interval)
    structure = _detect_structure(df, as_of=as_of, interval=interval)
    liquidity = _detect_liquidity(df, as_of=as_of, interval=interval)
    fvgs = _detect_fvgs(df, as_of=as_of, interval=interval)
    obs = _detect_order_blocks(df, as_of=as_of, interval=interval)
    return _build_entry_context(df, structure, liquidity, obs, fvgs, as_of=as_of, interval=interval)


def smc_text(smc):
    stc = smc["structure"]
    liq = smc["liquidity"]
    b = smc["bullish_entry"]
    s = smc["bearish_entry"]
    return "\n".join([
        f"STRUCTURE bias={stc['bias']} BOS={stc['bos']} CHOCH={stc['choch']}",
        f"SWING high={stc['last_swing_high']:.8f} low={stc['last_swing_low']:.8f}",
        f"LIQUIDITY sweep_high={liq['sweep_high']} sweep_low={liq['sweep_low']} "
        f"equal_high={liq['equal_high']} equal_low={liq['equal_low']}",
        f"BULL ENTRY zone={b['zone_low']}..{b['zone_high']} quality={b['quality']} "
        f"confluence={b['confluence']} status={b['status']}",
        f"BEAR ENTRY zone={s['zone_low']}..{s['zone_high']} quality={s['quality']} "
        f"confluence={s['confluence']} status={s['status']}",
        f"TIMING={smc['timing']} SMC_SCORE={smc['smc_score']}",
        f"DISPLACEMENT body_ratio={smc['displacement']['body_ratio']:.2f} "
        f"vol_ratio={smc['displacement']['volume_ratio']:.2f}",
    ])


def market_regime(d):
    if _feat_mod is not None:
        return _feat_mod.safe_market_regime(d)
    if d is None or len(d) == 0:
        return "SIDEWAYS"
    a = d.iloc[-1]
    vol = float(a.ATR / a.Close)
    ema_gap = abs(a.EMA20 - a.EMA50) / a.Close

    if vol >= 0.03:
        return "VOLATILE"
    if ema_gap < 0.005:
        return "SIDEWAYS"
    return "TRENDING"

# -------------------------
# Scanner
# -------------------------
def scanner_score(symbol, change, qvol, d, ob):
    if _scan_mod is not None:
        bi = _scan_mod.scanner_score_bidirectional(symbol, change, qvol, d, ob)
        return bi["score"], bi["why"]
    d_closed = _closed_df(d)
    if d_closed is None or len(d_closed) == 0:
        return 0, ["data tidak cukup"]
    return 50, []


def scan_markets(
    n=60,
    use_cache=True,
    cache=None,
    budget=None,
    as_of=None,
    return_report=False,
    min_quote_volume=2_000_000.0,
):
    progress = st.progress(0) if hasattr(st, "progress") else None
    status = st.empty() if hasattr(st, "empty") else None

    def _on_progress(curr, total, sym):
        if status is not None and hasattr(status, "write"):
            status.write(f"🔎 Memindai {sym} ({curr}/{max(total, 1)})")
        if progress is not None and hasattr(progress, "progress"):
            progress.progress(curr / max(total, 1))

    try:
        if budget is None and _scan_mod is not None and hasattr(globals().get("GLOBAL_REQUEST_BUDGET"), "reset_cycle"):
            GLOBAL_REQUEST_BUDGET.reset_cycle(max_requests_per_cycle=1000, max_weight_per_cycle=5000, preserve_cooldown=True)
        if _scan_mod is not None and hasattr(_scan_mod, "scan_markets_service"):
            res = _scan_mod.scan_markets_service(
                n=n,
                tickers_fetcher=lambda: bget("/api/v3/ticker/24hr"),
                kline_fetcher=lambda sym, tf, lim: klines(sym, tf, lim),
                order_book_fetcher=lambda sym: order_book(sym),
                cache=cache if cache is not None else GLOBAL_MARKET_CACHE,
                budget=budget if budget is not None else GLOBAL_REQUEST_BUDGET,
                as_of=as_of,
                use_cache=use_cache,
                kline_limit=260,
                min_quote_volume=min_quote_volume,
                progress_callback=_on_progress,
            )
        else:
            tickers = bget("/api/v3/ticker/24hr")
            stable_set = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "AEUR", "PAXG"}
            pool = []
            for t in tickers or []:
                sym = str(t.get("symbol", "")).upper().strip()
                if not sym.endswith("USDT") or sym[:-4] in stable_set:
                    continue
                qv = float(t.get("quoteVolume", 0) or 0)
                chg = float(t.get("priceChangePercent", 0) or 0)
                if qv < float(min_quote_volume):
                    continue
                act = min(qv / 20_000_000.0, 5.0) * 10.0 + min(abs(chg), 10.0) * 2.0
                pool.append((sym, chg, qv, act))
            pool.sort(key=lambda x: (x[3], x[2]), reverse=True)
            sel = pool[:max(int(n), 1)]
            cands = []
            for idx_s, (sym, chg, qv, _) in enumerate(sel, 1):
                _on_progress(idx_s, len(sel), sym)
                try:
                    d_kl = _closed_df(klines(sym, "15m", 260))
                    if len(d_kl) == 0:
                        continue
                    ob_d = order_book(sym)
                    sc, why_l = scanner_score(sym, chg, qv, d_kl, ob_d)
                    last_r = d_kl.iloc[-1]
                    cands.append({
                        "symbol": sym,
                        "change": chg,
                        "qvol": qv,
                        "vr": float(last_r.VOL_RATIO),
                        "buy": float(last_r.BUY_PRESSURE),
                        "imb": float(ob_d["imbalance"]),
                        "spread": float(ob_d["spread"]),
                        "rsi": float(last_r.RSI),
                        "score": sc,
                        "why": why_l,
                    })
                except Exception:
                    continue
            cands.sort(key=lambda x: (x["score"], x["qvol"]), reverse=True)
            res = {"candidates": cands, "long_shortlist": cands, "short_shortlist": cands, "diagnostics": {"succeeded_count": len(cands)}}
    finally:
        if progress is not None and hasattr(progress, "empty"):
            progress.empty()
        if status is not None and hasattr(status, "empty"):
            status.empty()

    if hasattr(st, "session_state") and isinstance(st.session_state, dict):
        st.session_state["last_scan_diagnostics"] = res["diagnostics"]
        st.session_state["scan_long_shortlist"] = res["long_shortlist"]
        st.session_state["scan_short_shortlist"] = res["short_shortlist"]

    if return_report:
        return res
    return res["candidates"]


# -------------------------
# AI engine — V5.3.1 Hierarchical Multi-Model Safe Mode
# -------------------------
ANALYST_MODEL = secret("ANALYST_MODEL") or "openai/gpt-oss-20b"
_raw_debate_model = str(secret("DEBATE_MODEL") or "").strip()
DEBATE_MODEL = "openai/gpt-oss-20b" if (not _raw_debate_model or _raw_debate_model == "qwen/qwen3.6-27b") else _raw_debate_model
RESEARCH_MODEL = secret("RESEARCH_MODEL") or "openai/gpt-oss-120b"
JUDGE_MODEL = secret("JUDGE_MODEL") or "openai/gpt-oss-120b"

GEMINI_ANALYST_MODEL = secret("GEMINI_ANALYST_MODEL") or "gemini-2.5-flash"
GEMINI_DEBATE_MODEL = secret("GEMINI_DEBATE_MODEL") or "gemini-2.5-flash"
GEMINI_RESEARCH_MODEL = secret("GEMINI_RESEARCH_MODEL") or "gemini-2.5-pro"
GEMINI_JUDGE_MODEL = secret("GEMINI_JUDGE_MODEL") or "gemini-2.5-pro"

GEMINI_MODEL_TIERS = {
    "analyst": GEMINI_ANALYST_MODEL,
    "debate": GEMINI_DEBATE_MODEL,
    "research": GEMINI_RESEARCH_MODEL,
    "judge": GEMINI_JUDGE_MODEL,
}

ANALYST_MAX_TOKENS = int(secret("ANALYST_MAX_TOKENS") or 700)
DEBATE_MAX_TOKENS = int(secret("DEBATE_MAX_TOKENS") or 700)
RESEARCH_MAX_TOKENS = int(secret("RESEARCH_MAX_TOKENS") or 900)
JUDGE_MAX_TOKENS = int(secret("JUDGE_MAX_TOKENS") or 1100)
TRADER_MAX_TOKENS = int(secret("TRADER_MAX_TOKENS") or 600)
RISK_MAX_TOKENS = int(secret("RISK_MAX_TOKENS") or 550)
MIN_COMPLETION_TOKENS = int(secret("MIN_COMPLETION_TOKENS") or 128)

MAX_AI_CALLS_PER_CANDIDATE = int(secret("MAX_AI_CALLS_PER_CANDIDATE") or 14)
MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE = int(
    secret("MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE") or MAX_AI_CALLS_PER_CANDIDATE
)
MAX_PHYSICAL_ATTEMPTS_PER_CALL = int(secret("MAX_PHYSICAL_ATTEMPTS_PER_CALL") or 2)
DEFAULT_DEBATE_ROUNDS = int(secret("DEFAULT_DEBATE_ROUNDS") or 1)
AI_HTTP_TIMEOUT_SECONDS = float(secret("AI_HTTP_TIMEOUT_SECONDS") or 20.0)
AI_CANDIDATE_DEADLINE_SECONDS = float(secret("AI_CANDIDATE_DEADLINE_SECONDS") or 90.0)
GROQ_MIN_DELAY = float(secret("GROQ_MIN_DELAY") or 1.0)
GROQ_RETRY_SECONDS = float(secret("GROQ_RETRY_SECONDS") or 30.0)
AI_PROVIDER_ORDER = ["groq"]

# PH01-03 & PH04-03 Deterministic Gate, Sizing & Mode Matrix Constants (F06, F16, F33, F38, T19)
MAX_SPREAD_PCT = float(secret("MAX_SPREAD_PCT") or 0.35)
MIN_NET_RR = float(secret("MIN_NET_RR") or 1.5)
ESTIMATED_ROUND_TRIP_COST_PCT = float(secret("ESTIMATED_ROUND_TRIP_COST_PCT") or 0.12)
PAPER_INITIAL_EQUITY = float(secret("PAPER_INITIAL_EQUITY") or 10000.0)
PAPER_RISK_FRACTION = float(secret("PAPER_RISK_FRACTION") or 0.005)
PAPER_FEE_PCT_ROUND_TRIP = float(secret("PAPER_FEE_PCT_ROUND_TRIP") or 0.08)
PAPER_SLIPPAGE_PCT_ROUND_TRIP = float(secret("PAPER_SLIPPAGE_PCT_ROUND_TRIP") or 0.04)
PAPER_MAX_POSITION_EXPOSURE_PCT = float(secret("PAPER_MAX_POSITION_EXPOSURE_PCT") or 0.25)
PAPER_MAX_TOTAL_EXPOSURE_PCT = float(secret("PAPER_MAX_TOTAL_EXPOSURE_PCT") or 0.80)
PAPER_MAX_TOTAL_RISK_FRACTION = float(secret("PAPER_MAX_TOTAL_RISK_FRACTION") or 0.02)
PAPER_MAX_OPEN_POSITIONS = int(secret("PAPER_MAX_OPEN_POSITIONS") or 4)
PAPER_INSTRUMENT_MODE = str(secret("PAPER_INSTRUMENT_MODE") or "spot_reference")
MIN_JUDGE_CONFIDENCE = float(secret("MIN_JUDGE_CONFIDENCE") or 55.0)
LEGACY_ACCOUNTING_MODE = "LEGACY_R_SNAPSHOT_ONLY"
if "REALISTIC_ACCOUNTING_MODE" not in globals():
    REALISTIC_ACCOUNTING_MODE = "V6_REALISTIC_PAPER_LEDGER"
if "REALISTIC_ACCOUNTING_QUALITY" not in globals():
    REALISTIC_ACCOUNTING_QUALITY = "v6_realistic_ledger"

MODE_TIMEFRAME_MATRIX = {
    "scalping": {
        "bias_tf": "1h",
        "setup_tf": "15m",
        "trigger_tf": "5m",
        "required_timeframes": ("5m", "15m", "1h"),
    },
    "intraday": {
        "bias_tf": "4h",
        "setup_tf": "1h",
        "trigger_tf": "15m",
        "required_timeframes": ("15m", "1h", "4h"),
    },
    "swing": {
        "bias_tf": "1d",
        "setup_tf": "4h",
        "trigger_tf": "1h",
        "required_timeframes": ("1h", "4h", "1d"),
    },
}

MODEL_TIERS = {
    "analyst": {
        "model": ANALYST_MODEL,
        "max_tokens": ANALYST_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, ANALYST_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "debate": {
        "model": DEBATE_MODEL,
        "max_tokens": DEBATE_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, DEBATE_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "research": {
        "model": RESEARCH_MODEL,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, RESEARCH_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "judge": {
        "model": JUDGE_MODEL,
        "max_tokens": JUDGE_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, JUDGE_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
}

AI_SCHEMA = AI_JUDGE_SCHEMA if _agt_mod is not None else {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "key_risk": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "modes": {
            "type": "object",
            "properties": {
                "scalping": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
                "intraday": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
                "swing": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
            },
            "required": ["scalping", "intraday", "swing"],
            "additionalProperties": False,
        },
    },
    "required": ["decision", "confidence", "reasoning", "key_risk", "evidence", "modes"],
    "additionalProperties": False,
}

_REQUIRED_AI_FIELDS = ("decision", "confidence", "reasoning", "key_risk", "evidence")
_ALLOWED_AI_FIELDS = {"decision", "confidence", "reasoning", "key_risk", "evidence", "modes"}
_ALLOWED_DECISIONS = {"LONG", "SHORT", "WAIT"}
_ALLOWED_MODES = ("scalping", "intraday", "swing")
_REQUIRED_MODE_FIELDS = {"decision", "confidence", "reason"}

# Thread-safe atomic quota scope reservation lock & ledger (PH01-02 / F02 / T02)
_QUOTA_SCOPE_LOCK = threading.Lock()
_QUOTA_SCOPE_BUDGETS = {}


def reset_quota_scope_budget(scope_id="groq_default", max_physical_attempts=None, deadline_epoch=None):
    """Initialize or reset a shared quota scope budget for atomic physical attempt reservation."""
    with _QUOTA_SCOPE_LOCK:
        _QUOTA_SCOPE_BUDGETS[scope_id] = {
            "physical_used": 0,
            "logical_used": 0,
            "max_physical": int(max_physical_attempts) if max_physical_attempts is not None else None,
            "deadline_epoch": float(deadline_epoch) if deadline_epoch is not None else None,
            "exhausted_until": 0.0,
            "exhausted_reason": "",
        }
        return dict(_QUOTA_SCOPE_BUDGETS[scope_id])


def get_quota_scope_state(scope_id="groq_default"):
    with _QUOTA_SCOPE_LOCK:
        entry = _QUOTA_SCOPE_BUDGETS.get(scope_id)
        return dict(entry) if entry else {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""}


def _mark_quota_scope_exhausted(scope_id="groq_default", seconds=86400.0, reason="DAILY_QUOTA_EXHAUSTED"):
    with _QUOTA_SCOPE_LOCK:
        scope = _QUOTA_SCOPE_BUDGETS.setdefault(
            scope_id,
            {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""},
        )
        scope["exhausted_until"] = time.time() + float(seconds)
        scope["exhausted_reason"] = str(reason)
    if _stg_mod is not None:
        try:
            GLOBAL_DURABLE_BUDGET_MANAGER.mark_scope_exhausted(scope_id=scope_id, seconds=seconds, reason=reason, db_file=DB_FILE)
        except Exception:
            pass


def _resolve_physical_cap(explicit_cap=None):
    if explicit_cap is not None:
        return max(0, int(explicit_cap))
    return max(0, min(int(MAX_AI_CALLS_PER_CANDIDATE), int(MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE)))


def _reserve_physical_attempt(scope_id="groq_default", max_physical_attempts=None, now_fn=time.time):
    """Atomically check and reserve 1 physical transport attempt across threads/workers, processes, and session state."""
    now = float(now_fn())
    if _stg_mod is not None:
        try:
            is_exh, exh_reason, _ = GLOBAL_DURABLE_BUDGET_MANAGER.is_scope_exhausted(scope_id=scope_id, now_fn=now_fn, db_file=DB_FILE)
            if is_exh:
                return False, f"QUOTA_SCOPE_EXHAUSTED:{exh_reason}", 0, _resolve_physical_cap(max_physical_attempts)
        except Exception:
            pass
    with _QUOTA_SCOPE_LOCK:
        scope = _QUOTA_SCOPE_BUDGETS.setdefault(
            scope_id,
            {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""},
        )
        effective_cap = (
            int(max_physical_attempts)
            if max_physical_attempts is not None
            else (
                int(scope["max_physical"])
                if scope.get("max_physical") is not None
                else _resolve_physical_cap()
            )
        )
        if float(scope.get("exhausted_until", 0.0) or 0.0) > now:
            return False, f"QUOTA_SCOPE_EXHAUSTED:{scope.get('exhausted_reason', 'DAILY_QUOTA_EXHAUSTED')}", scope["physical_used"], effective_cap
        deadline = scope.get("deadline_epoch")
        if deadline is not None and now > float(deadline):
            return False, "DEADLINE_EXCEEDED", scope["physical_used"], effective_cap

        sess_used = int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0)
        current_used = max(int(scope["physical_used"]), sess_used)
        if current_used >= effective_cap:
            return False, "PHYSICAL_CAP_REACHED", current_used, effective_cap

        new_used = current_used + 1
        scope["physical_used"] = new_used
        st.session_state["_ai_physical_attempts_current_candidate"] = new_used
        return True, "OK", new_used, effective_cap


def resolve_effective_max_tokens(tier, requested_budget=None):
    """Explicitly resolve completion token cap without a hidden 384 floor override (F20 / T04)."""
    cfg = MODEL_TIERS.get(tier, MODEL_TIERS["analyst"])
    raw_target = int(requested_budget) if requested_budget is not None else int(cfg["max_tokens"])
    floor = max(1, int(MIN_COMPLETION_TOKENS))
    return max(floor, raw_target)


def _is_finite_number(value):
    """Return True iff value is an int/float (not bool) and math.isfinite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _is_valid_finite_confidence(value):
    """Strictly validate that confidence is a finite int/float in [0, 100], rejecting bool, str, NaN, and ±Infinity (F03 / T03)."""
    if not _is_finite_number(value):
        return False
    val = float(value)
    return 0.0 <= val <= 100.0


def _sanitize_confidence_for_summary(value):
    if _is_valid_finite_confidence(value):
        return round(float(value), 2)
    return 0


def compact_json(obj, max_chars=1400):
    """Serialize a compact summary as guaranteed-valid JSON within max_chars by trimming fields before serialization (F19 / T04)."""
    max_chars = max(72, int(max_chars))
    if not isinstance(obj, dict):
        obj = {"decision": "WAIT", "confidence": 0, "reasoning": str(obj or ""), "key_risk": "", "evidence": []}

    raw_decision = obj.get("decision", "WAIT")
    decision = str(raw_decision).upper().strip() if isinstance(raw_decision, str) else "WAIT"
    if decision not in _ALLOWED_DECISIONS:
        decision = "WAIT"

    confidence = _sanitize_confidence_for_summary(obj.get("confidence", 0))
    reasoning = str(obj.get("reasoning", "") or "")
    key_risk = str(obj.get("key_risk", "") or "")
    raw_ev = obj.get("evidence", [])
    if not isinstance(raw_ev, list):
        raw_ev = [str(raw_ev)] if raw_ev else []
    evidence = [str(x) for x in raw_ev[:3] if x is not None]

    def _dump(r_text, k_text, ev_list):
        payload = {
            "decision": decision,
            "confidence": confidence,
            "reasoning": r_text,
            "key_risk": k_text,
            "evidence": ev_list,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    full = _dump(reasoning, key_risk, evidence)
    if len(full) <= max_chars:
        return full

    if len(_dump("", "", [])) > max_chars and isinstance(confidence, float):
        confidence = int(round(confidence))

    trimmed_ev = list(evidence)
    while trimmed_ev and len(_dump("", "", trimmed_ev)) > max(max_chars - 20, 72):
        if len(trimmed_ev[-1]) > 16:
            trimmed_ev[-1] = trimmed_ev[-1][: max(1, len(trimmed_ev[-1]) // 2)]
        else:
            trimmed_ev.pop()

    if len(_dump(reasoning, key_risk, trimmed_ev)) <= max_chars:
        return _dump(reasoning, key_risk, trimmed_ev)

    while trimmed_ev and len(_dump(reasoning[:12], key_risk[:12], trimmed_ev)) > max_chars:
        trimmed_ev.pop()

    lo = 0
    hi = max(len(reasoning), len(key_risk), 1)
    best_json = _dump("", "", [])
    while lo <= hi:
        mid = (lo + hi) // 2
        r_slice = reasoning[:mid]
        k_slice = key_risk[: max(0, mid // 2)]
        candidate_str = _dump(r_slice, k_slice, trimmed_ev)
        if len(candidate_str) <= max_chars:
            best_json = candidate_str
            lo = mid + 1
        else:
            hi = mid - 1

    json.loads(best_json)
    return best_json


def _provider_state(name):
    key = f"{name}_key_state"
    if key not in st.session_state:
        keys = GROQ_KEYS if name == "groq" else (GEMINI_KEYS if name == "gemini" else [])
        st.session_state[key] = {
            i: {"disabled_until": 0.0, "reason": "", "failures": 0}
            for i in range(len(keys))
        }
    return st.session_state[key]


def _key_mask(key):
    if not key:
        return "-"
    return f"{key[:5]}...{key[-4:]}" if len(key) > 12 else "***"


def _key_disabled(provider, index):
    return float(_provider_state(provider).get(index, {}).get("disabled_until", 0)) > time.time()


def _disable_key(provider, index, seconds, reason):
    state = _provider_state(provider).setdefault(
        index, {"disabled_until": 0.0, "reason": "", "failures": 0}
    )
    state["disabled_until"] = time.time() + seconds
    state["reason"] = str(reason)[:180]
    state["failures"] = int(state.get("failures", 0)) + 1


def _is_quota_error(message):
    msg = str(message).lower()
    return any(x in msg for x in (
        "tokens per day", "requests per day", "daily limit", "daily quota",
        "quota exceeded", "quota has been exceeded", "insufficient_quota",
        "billing", "resource_exhausted"
    ))


def _is_rate_limit_error(message):
    msg = str(message).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg or "too many requests" in msg


def _is_auth_error(message):
    msg = str(message).lower()
    return any(x in msg for x in ("401", "403", "invalid api key", "authentication", "unauthorized"))


def _is_model_not_found(message):
    msg = str(message).lower()
    return "model_not_found" in msg or "does not exist" in msg or "do not have access" in msg


def _build_prompt(role, data, instructions, tier="analyst", include_modes=None):
    _extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _agt_mod is not None:
        return _agt_mod.build_agent_prompt(
            role,
            data,
            instructions,
            tier=tier,
            include_modes=include_modes,
            extra_secrets=_extra_s,
        )
    if _cfg_mod is not None:
        data = redact_secrets(str(data or ""), extra_secrets=_extra_s)
        instructions = redact_secrets(str(instructions or ""), extra_secrets=_extra_s)
    return f"""
Kamu adalah {role} dalam sistem riset trading multi-agent.

TUGAS:
{instructions}

ATURAN WAJIB:
1. Gunakan hanya data yang diberikan. Jangan mengarang fakta.
2. Jika bukti tidak cukup, pilih WAIT.
3. Jawab dalam Bahasa Indonesia.
4. Jawaban akhir WAJIB berupa SATU OBJECT JSON VALID.
5. JSON harus memiliki tepat struktur berikut:
{{"decision":"LONG|SHORT|WAIT","confidence":0,"reasoning":"alasan singkat","key_risk":"risiko utama","evidence":["bukti 1","bukti 2"]}}
6. decision hanya LONG, SHORT, atau WAIT.
7. confidence adalah angka finite 0 sampai 100 (bukan NaN/Infinity/string/boolean).
8. evidence adalah array string, maksimal 3 item.
9. Jangan gunakan markdown, jangan gunakan ```json, dan jangan menulis teks sebelum/sesudah JSON.
10. Kata JSON di atas berarti output akhir harus benar-benar JSON yang dapat diparse Python.

DATA:
{data}
"""


def _reject_non_finite_constant(constant_str):
    raise ValueError(f"Konstanta JSON non-finite tidak diizinkan: {constant_str}")


def _safe_json_from_text(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Respons AI kosong.")

    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.I).replace("```", "").strip()

    try:
        obj = json.loads(cleaned, parse_constant=_reject_non_finite_constant)
        if isinstance(obj, dict):
            return obj
    except ValueError as ve:
        if "Konstanta JSON non-finite" in str(ve):
            raise
    except Exception:
        pass

    starts = [m.start() for m in re.finditer(r"\{", cleaned)]
    for start_pos in starts:
        depth = 0
        in_string = False
        escape = False
        for i in range(start_pos, len(cleaned)):
            ch = cleaned[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start_pos:i+1]
                    try:
                        obj = json.loads(candidate, parse_constant=_reject_non_finite_constant)
                        if isinstance(obj, dict):
                            return obj
                    except ValueError as ve:
                        if "Konstanta JSON non-finite" in str(ve):
                            raise
                        break
                    except Exception:
                        break
    raise ValueError("Respons AI bukan JSON valid.")


def _extract_usage(raw_response):
    """Extract token usage metadata from provider response without fabricating zero-cost when usage is missing (F20 / T04)."""
    usage_obj = getattr(raw_response, "usage", None) if raw_response is not None else None
    if usage_obj is None and isinstance(raw_response, dict):
        usage_obj = raw_response.get("usage")

    if usage_obj is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_known": False,
            "cost_status": "UNKNOWN_USAGE",
        }

    def _get_int(src, *names):
        for n in names:
            val = getattr(src, n, None) if not isinstance(src, dict) else src.get(n)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                return val
        return None

    in_tok = _get_int(usage_obj, "prompt_tokens", "input_tokens")
    out_tok = _get_int(usage_obj, "completion_tokens", "output_tokens")
    tot_tok = _get_int(usage_obj, "total_tokens")
    if tot_tok is None and in_tok is not None and out_tok is not None:
        tot_tok = in_tok + out_tok

    details = (
        getattr(usage_obj, "completion_tokens_details", None)
        if not isinstance(usage_obj, dict)
        else usage_obj.get("completion_tokens_details")
    )
    reasoning_tok = _get_int(details, "reasoning_tokens") if details is not None else None
    known = any(x is not None for x in (in_tok, out_tok, tot_tok))

    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": reasoning_tok,
        "total_tokens": tot_tok,
        "usage_known": bool(known),
        "cost_status": "MEASURED" if known else "UNKNOWN_USAGE",
    }


def _invalid_ai_output(reason, tier, provider="unknown", model=None, finish_reason=None, usage=None, requested_max_tokens=None, effective_max_tokens=None):
    cfg = MODEL_TIERS.get(tier, MODEL_TIERS["analyst"])
    eff_tok = effective_max_tokens if effective_max_tokens is not None else resolve_effective_max_tokens(tier, requested_max_tokens)
    req_tok = requested_max_tokens if requested_max_tokens is not None else cfg["max_tokens"]
    return {
        "status": "INVALID_OUTPUT",
        "decision": "WAIT",
        "confidence": 0.0,
        "reasoning": f"Output AI ditolak validator (INVALID_OUTPUT): {str(reason)[:500]}",
        "key_risk": "Output AI tidak memenuhi skema atau batas numerik finite.",
        "evidence": [],
        "modes": {},
        "tier": tier,
        "provider": provider,
        "model": model or cfg["model"],
        "finish_reason": finish_reason,
        "usage": usage or _extract_usage(None),
        "requested_max_tokens": int(req_tok),
        "effective_max_tokens": int(eff_tok),
    }


def _normalize_ai(
    obj,
    tier,
    provider="unknown",
    model=None,
    finish_reason=None,
    usage=None,
    requested_max_tokens=None,
    effective_max_tokens=None,
    strict=True,
    role="",
    strict_role_schema=False,
):
    """Validate and normalize AI output; reject non-finite numbers, invalid types, missing/extra fields, invalid enums, or length truncation (F03, F18, F20, T03)."""
    if tier not in MODEL_TIERS:
        tier = "analyst"
    cfg = MODEL_TIERS[tier]
    eff_tok = effective_max_tokens if effective_max_tokens is not None else resolve_effective_max_tokens(tier, requested_max_tokens)
    req_tok = requested_max_tokens if requested_max_tokens is not None else cfg["max_tokens"]
    norm_usage = usage if isinstance(usage, dict) else _extract_usage(None)

    if str(finish_reason or "").strip().lower() == "length":
        return _invalid_ai_output(
            "Respons terpotong karena batas token (finish_reason=length).",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    if not isinstance(obj, dict):
        return _invalid_ai_output(
            "Output AI bukan object JSON.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    missing = [k for k in _REQUIRED_AI_FIELDS if k not in obj]
    if missing:
        return _invalid_ai_output(
            f"Field wajib tidak lengkap: {', '.join(missing)}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    if strict:
        judge_flag = (_agt_mod.is_judge_role(role, tier) if _agt_mod is not None else (tier == "judge"))
        if strict_role_schema:
            allowed_fields = _ALLOWED_AI_FIELDS if judge_flag else set(_REQUIRED_AI_FIELDS)
            if judge_flag and "modes" not in obj:
                return _invalid_ai_output(
                    "Final Judge wajib menyertakan field modes (scalping, intraday, swing).",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
        else:
            allowed_fields = _ALLOWED_AI_FIELDS
        extra = set(obj.keys()) - allowed_fields
        if extra:
            return _invalid_ai_output(
                f"Field ekstra tidak dikenal: {', '.join(sorted(extra))}",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )

    raw_decision = obj.get("decision")
    if not isinstance(raw_decision, str):
        return _invalid_ai_output(
            "Field decision harus berupa string.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )
    decision = raw_decision.strip().upper()
    if decision not in _ALLOWED_DECISIONS:
        return _invalid_ai_output(
            f"Enum decision tidak valid: {raw_decision}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    raw_conf = obj.get("confidence")
    if not _is_valid_finite_confidence(raw_conf):
        return _invalid_ai_output(
            f"Nilai confidence tidak valid atau non-finite: {raw_conf!r}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )
    confidence = float(raw_conf)

    reasoning = obj.get("reasoning")
    key_risk = obj.get("key_risk")
    if not isinstance(reasoning, str) or not isinstance(key_risk, str):
        return _invalid_ai_output(
            "Field reasoning dan key_risk harus berupa string.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    evidence = obj.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        return _invalid_ai_output(
            "Field evidence harus berupa array of strings.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    modes = obj.get("modes")
    normalized_modes = {}
    if modes is not None:
        if not isinstance(modes, dict):
            return _invalid_ai_output(
                "Field modes harus berupa object.",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )
        extra_modes = set(modes.keys()) - set(_ALLOWED_MODES)
        if strict and extra_modes:
            return _invalid_ai_output(
                f"Mode tidak dikenal: {', '.join(sorted(extra_modes))}",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )
        for mode_name in _ALLOWED_MODES:
            if mode_name not in modes:
                continue
            m = modes.get(mode_name)
            if not isinstance(m, dict):
                return _invalid_ai_output(
                    f"Mode {mode_name} harus berupa object.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if not _REQUIRED_MODE_FIELDS.issubset(set(m.keys())):
                return _invalid_ai_output(
                    f"Mode {mode_name} kehilangan field wajib.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if strict and (set(m.keys()) - _REQUIRED_MODE_FIELDS):
                return _invalid_ai_output(
                    f"Mode {mode_name} memiliki field ekstra tidak dikenal.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            raw_md = m.get("decision")
            raw_mc = m.get("confidence")
            raw_mr = m.get("reason")
            if not isinstance(raw_md, str) or raw_md.strip().upper() not in _ALLOWED_DECISIONS:
                return _invalid_ai_output(
                    f"Decision mode {mode_name} tidak valid: {raw_md!r}",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if not _is_valid_finite_confidence(raw_mc) or not isinstance(raw_mr, str):
                return _invalid_ai_output(
                    f"Confidence/reason mode {mode_name} tidak valid.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            normalized_modes[mode_name] = {
                "decision": raw_md.strip().upper(),
                "confidence": float(raw_mc),
                "reason": raw_mr[:240],
            }

    return {
        "status": "OK",
        "decision": decision,
        "confidence": confidence,
        "reasoning": reasoning[:700],
        "key_risk": key_risk[:400],
        "evidence": [x[:180] for x in evidence[:3]],
        "modes": normalized_modes,
        "tier": tier,
        "provider": provider,
        "model": model or cfg["model"],
        "finish_reason": finish_reason,
        "usage": norm_usage,
        "requested_max_tokens": int(req_tok),
        "effective_max_tokens": int(eff_tok),
    }


def _groq_client(key):
    """Single retry owner: disable internal SDK retries (max_retries=0) and enforce explicit HTTP timeout (F02 / T02)."""
    try:
        return Groq(api_key=key, max_retries=0, timeout=AI_HTTP_TIMEOUT_SECONDS)
    except TypeError:
        return Groq(api_key=key)


def _is_groq_schema_400_error(exc_or_msg):
    msg_l = str(exc_or_msg or "").lower()
    return any(
        k in msg_l
        for k in (
            "generated json does not match the expected schema",
            "json_validate_failed",
            "error: jsonschema:",
            "does not validate with /required",
            "failed_generation",
        )
    )


def _request_groq(model, prompt, key, max_tokens, tier, role="", strict_registry=True):
    """Groq request with capability registry check, explicit token budget, role-specific JSON schema, and automatic json_object fallback on Groq 400 schema/reasoning-truncation errors (PH05-01, F18, F20, T04, T21)."""
    effective_tokens = resolve_effective_max_tokens(tier, max_tokens)
    is_judge = (_agt_mod.is_judge_role(role, tier) if _agt_mod is not None else (str(tier).lower() == "judge" or "judge" in str(role).lower() or "portfolio" in str(role).lower()))
    if _prov_mod is not None:
        req_bundle = _prov_mod.build_provider_request_payload(
            role=role,
            tier=tier,
            model_id=model,
            prompt=prompt,
            max_completion_tokens=effective_tokens,
            provider="groq",
            reasoning_effort=MODEL_TIERS.get(tier, {}).get("reasoning_effort", "low"),
            strict_registry=strict_registry,
        )
        sdk_kwargs = dict(req_bundle["sdk_kwargs"])
    else:
        base_5f_schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
                "key_risk": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["decision", "confidence", "reasoning", "key_risk", "evidence"],
            "additionalProperties": False,
        }
        target_schema = (_agt_mod.get_schema_for_role(role, tier) if _agt_mod is not None else (AI_SCHEMA if is_judge else base_5f_schema))
        schema_name = "trading_judge_decision" if is_judge else "trading_agent_decision"
        sdk_kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_completion_tokens": effective_tokens,
        }
        if model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
            sdk_kwargs["reasoning_effort"] = MODEL_TIERS[tier].get("reasoning_effort", "low")
            sdk_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": target_schema,
                },
            }
        elif model.startswith("qwen/"):
            sdk_kwargs["reasoning_effort"] = "none"
            sdk_kwargs["response_format"] = {"type": "json_object"}
        else:
            sdk_kwargs["response_format"] = {"type": "json_object"}

    client = _groq_client(key)
    try:
        raw_resp = client.chat.completions.create(**sdk_kwargs)
    except Exception as exc:
        if (
            isinstance(sdk_kwargs.get("response_format"), dict)
            and sdk_kwargs["response_format"].get("type") == "json_schema"
            and _is_groq_schema_400_error(exc)
        ):
            fallback_kwargs = dict(sdk_kwargs)
            fallback_kwargs["response_format"] = {"type": "json_object"}
            fallback_kwargs["max_completion_tokens"] = max(int(effective_tokens), 1200 if is_judge else 850)
            return client.chat.completions.create(**fallback_kwargs)
        if _is_model_not_found(exc):
            fallback_models = (
                ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]
                if str(tier).lower() in {"research", "judge"} or is_judge
                else ["openai/gpt-oss-20b", "qwen/qwen3-32b", "llama-3.3-70b-versatile"]
            )
            for fb_model in fallback_models:
                if fb_model == model:
                    continue
                fb_kwargs = {
                    "model": fb_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_completion_tokens": max(int(effective_tokens), 1100 if is_judge else 800),
                    "response_format": {"type": "json_object"},
                }
                if fb_model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
                    fb_kwargs["reasoning_effort"] = "low"
                elif fb_model.startswith("qwen/"):
                    fb_kwargs["reasoning_effort"] = "none"
                try:
                    return client.chat.completions.create(**fb_kwargs)
                except Exception as fb_exc:
                    if _is_model_not_found(fb_exc):
                        continue
                    raise
        raise

    if model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"} and getattr(raw_resp, "choices", None):
        first_ch = raw_resp.choices[0]
        msg_obj = getattr(first_ch, "message", None)
        c_txt = (getattr(msg_obj, "content", None) or "").strip()
        r_txt = (getattr(msg_obj, "reasoning", None) or "").strip()
        if not c_txt and r_txt:
            fallback_kwargs = dict(sdk_kwargs)
            fallback_kwargs["response_format"] = {"type": "json_object"}
            fallback_kwargs["max_completion_tokens"] = max(int(effective_tokens) * 2, 1200 if is_judge else 900)
            try:
                return client.chat.completions.create(**fallback_kwargs)
            except Exception:
                return raw_resp

    return raw_resp


def _request_gemini(model, prompt, key, max_tokens, tier, role="", strict_registry=True):
    """Optional Gemini fallback request enforcing capability registry and role-specific schema (PH05-04, T21)."""
    effective_tokens = resolve_effective_max_tokens(tier, max_tokens)
    payload_bundle = (
        _prov_mod.build_gemini_http_payload(
            role=role,
            tier=tier,
            model_id=model,
            prompt=prompt,
            max_completion_tokens=effective_tokens,
            strict_registry=strict_registry,
        )
        if _prov_mod is not None
        else {"http_body": {"contents": [{"parts": [{"text": prompt}]}]}}
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(
        url,
        params={"key": key},
        json=payload_bundle["http_body"],
        timeout=AI_HTTP_TIMEOUT_SECONDS,
    )
    if r.status_code != 200:
        err_exc = RuntimeError(f"Gemini HTTP {r.status_code}: {(r.text or '')[:300]}")
        err_exc.status_code = r.status_code
        err_exc.headers = dict(getattr(r, "headers", {}) or {})
        raise err_exc
    data = r.json()
    cand_list = data.get("candidates") or []
    first_cand = cand_list[0] if cand_list else {}
    parts = first_cand.get("content", {}).get("parts") or []
    text_out = parts[0].get("text", "") if parts else ""
    finish_r = str(first_cand.get("finishReason", "STOP")).lower()
    if finish_r == "max_tokens":
        finish_r = "length"
    u_meta = data.get("usageMetadata") or {}

    class _GemMsg:
        def __init__(self, c):
            self.content = c
            self.reasoning = None

    class _GemChoice:
        def __init__(self, c, fr):
            self.message = _GemMsg(c)
            self.finish_reason = fr

    class _GemResp:
        def __init__(self, c, fr, um):
            self.choices = [_GemChoice(c, fr)]
            self.usage = (
                {
                    "prompt_tokens": um.get("promptTokenCount"),
                    "completion_tokens": um.get("candidatesTokenCount"),
                    "total_tokens": um.get("totalTokenCount"),
                }
                if um
                else None
            )

    return _GemResp(text_out, finish_r, u_meta)


def _ordered_key_indices(provider):
    if provider == "groq":
        if not GROQ_KEYS:
            return []
        active = int(st.session_state.get("_active_groq_key", 0)) % len(GROQ_KEYS)
        return list(range(active, len(GROQ_KEYS))) + list(range(0, active))
    if provider == "gemini":
        if not GEMINI_KEYS:
            return []
        active = int(st.session_state.get("_active_gemini_key", 0)) % len(GEMINI_KEYS)
        return list(range(active, len(GEMINI_KEYS))) + list(range(0, active))
    return []


def _provider_models(provider, tier):
    if provider == "groq":
        return [MODEL_TIERS[tier]["model"]]
    if provider == "gemini":
        return [GEMINI_MODEL_TIERS.get(tier, "gemini-2.5-flash")]
    return []


def call_ai(
    role,
    data,
    instructions,
    tier="analyst",
    budget=None,
    quota_scope="groq_default",
    max_physical_attempts=None,
    deadline_epoch=None,
    analysis_id=None,
    budget_manager=None,
):
    if tier not in MODEL_TIERS:
        tier = "analyst"

    cfg = MODEL_TIERS[tier]
    requested_tokens = int(budget) if budget is not None else int(cfg["max_tokens"])
    effective_tokens = resolve_effective_max_tokens(tier, requested_tokens)

    physical_cap = _resolve_physical_cap(max_physical_attempts)
    with _QUOTA_SCOPE_LOCK:
        if "_ai_calls_current_candidate" not in st.session_state:
            st.session_state["_ai_calls_current_candidate"] = 0
        if "_ai_physical_attempts_current_candidate" not in st.session_state:
            st.session_state["_ai_physical_attempts_current_candidate"] = 0

        logical_used = int(st.session_state.get("_ai_calls_current_candidate", 0) or 0)
        physical_used = int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0)

        if logical_used >= int(MAX_AI_CALLS_PER_CANDIDATE) or physical_used >= physical_cap:
            return {
                "status": "BUDGET_EXHAUSTED",
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": "Batas panggilan/percobaan fisik AI per kandidat tercapai.",
                "key_risk": "Quota dan batas percobaan fisik dihemat.",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": "none",
                "model": "",
                "finish_reason": None,
                "usage": _extract_usage(None),
                "requested_max_tokens": requested_tokens,
                "effective_max_tokens": effective_tokens,
            }

        st.session_state["_ai_calls_current_candidate"] = logical_used + 1

    call_deadline = (
        float(deadline_epoch)
        if deadline_epoch is not None
        else float(st.session_state.get("_ai_candidate_deadline", 0) or 0)
    )
    prompt = _build_prompt(role, data, instructions, tier=tier)
    errors = []
    attempts_this_call = 0
    per_call_limit = max(1, int(MAX_PHYSICAL_ATTEMPTS_PER_CALL))
    last_err_category = None
    durable_mgr = budget_manager or (GLOBAL_DURABLE_BUDGET_MANAGER if _stg_mod is not None else None)
    durable_res_id = None

    if durable_mgr is not None and analysis_id:
        reserved_durable, res_reason_durable, durable_res_id = durable_mgr.reserve_attempt(
            analysis_id=analysis_id,
            scope_id=quota_scope,
            role=role,
            tier=tier,
            requested_budget=effective_tokens,
            max_physical_attempts=physical_cap,
            deadline_epoch=(call_deadline if call_deadline > 0 else None),
            db_file=DB_FILE,
        )
        if not reserved_durable:
            return {
                "status": "BUDGET_EXHAUSTED",
                "error_category": res_reason_durable.split(":")[0],
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": f"Batas kuota/percobaan fisik tercapai ({res_reason_durable}).",
                "key_risk": "Quota scope dan Final Judge terlindungi.",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": "none",
                "model": "",
                "finish_reason": None,
                "usage": _extract_usage(None),
                "requested_max_tokens": requested_tokens,
                "effective_max_tokens": effective_tokens,
            }

    providers = []
    if GROQ_KEYS:
        providers.append("groq")
    if GEMINI_ENABLED and GEMINI_KEYS:
        providers.append("gemini")

    abort_fallback_providers = False

    for provider in providers:
        if abort_fallback_providers:
            break
        keys = GROQ_KEYS if provider == "groq" else (GEMINI_KEYS if provider == "gemini" else [])
        if not keys:
            continue
        provider_scope = quota_scope if provider == "groq" else f"{provider}_default"

        for model in _provider_models(provider, tier):
            if _prov_mod is not None:
                try:
                    _prov_mod.get_model_capability(model, provider=provider, strict_registry=True)
                except _prov_mod.ProviderModelUnsupportedError as cap_err:
                    last_err_category = "MODEL_UNSUPPORTED"
                    errors.append(f"{provider} / {model}: {str(cap_err)[:240]}")
                    abort_fallback_providers = True
                    break
            for key_index in _ordered_key_indices(provider):
                if attempts_this_call >= per_call_limit:
                    break
                if _key_disabled(provider, key_index):
                    continue
                if call_deadline > 0 and time.time() > call_deadline:
                    errors.append("Deadline kandidat terlampaui sebelum request.")
                    break

                reserved, reserve_reason, _, _ = _reserve_physical_attempt(
                    scope_id=provider_scope if provider != "groq" and get_quota_scope_state(quota_scope).get("exhausted_until", 0.0) > time.time() else quota_scope,
                    max_physical_attempts=max_physical_attempts,
                )
                if not reserved:
                    errors.append(f"Reservasi fisik ditolak: {reserve_reason}")
                    break

                attempts_this_call += 1
                key = keys[key_index]
                try:
                    last = float(st.session_state.get("_last_ai_call", 0) or 0)
                    wait = GROQ_MIN_DELAY - (time.time() - last)
                    if wait > 0:
                        time.sleep(wait)
                    st.session_state["_last_ai_call"] = time.time()
                    if provider == "gemini":
                        try:
                            raw = _request_gemini(model, prompt, key, effective_tokens, tier, role=role)
                        except TypeError:
                            raw = _request_gemini(model, prompt, key, effective_tokens, tier)
                        st.session_state["_active_gemini_key"] = key_index
                    else:
                        try:
                            raw = _request_groq(model, prompt, key, effective_tokens, tier, role=role)
                        except TypeError:
                            raw = _request_groq(model, prompt, key, effective_tokens, tier)
                        st.session_state["_active_groq_key"] = key_index

                    choice = raw.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    usage_info = _extract_usage(raw)
                    message = choice.message
                    content = message.content or ""

                    if str(finish_reason or "").strip().lower() == "length":
                        if durable_mgr is not None and durable_res_id:
                            durable_mgr.commit_usage(durable_res_id, usage_info, status_str="INVALID_OUTPUT", role=role, provider=provider, model_id=model, finish_reason=finish_reason, db_file=DB_FILE)
                        return _invalid_ai_output(
                            "Respons terpotong karena batas token (finish_reason=length).",
                            tier,
                            provider=provider,
                            model=model,
                            finish_reason=finish_reason,
                            usage=usage_info,
                            requested_max_tokens=requested_tokens,
                            effective_max_tokens=effective_tokens,
                        )

                    if not content.strip():
                        reasoning = getattr(message, "reasoning", None) or ""
                        if reasoning:
                            raise RuntimeError(
                                "Respons AI kosong (reasoning ada tetapi content JSON tidak keluar)."
                            )
                        raise RuntimeError("Respons AI kosong.")

                    obj = _safe_json_from_text(content)
                    normalized = _normalize_ai(
                        obj,
                        tier,
                        provider=provider,
                        model=model,
                        finish_reason=finish_reason,
                        usage=usage_info,
                        requested_max_tokens=requested_tokens,
                        effective_max_tokens=effective_tokens,
                        role=role,
                    )
                    if durable_mgr is not None and durable_res_id:
                        durable_mgr.commit_usage(durable_res_id, usage_info, status_str=normalized.get("status", "OK"), role=role, provider=provider, model_id=model, finish_reason=finish_reason, db_file=DB_FILE)
                    if normalized.get("status") != "OK":
                        return normalized
                    return normalized

                except Exception as e:
                    msg = str(e)
                    errors.append(f"{provider} {_key_mask(key)} / {model}: {msg[:240]}")

                    if _prov_mod is not None:
                        typed_err = _prov_mod.coerce_to_provider_error(
                            e,
                            provider=provider,
                            model_id=model,
                            default_retry_seconds=GROQ_RETRY_SECONDS,
                        )
                        last_err_category = typed_err.category
                        if typed_err.disable_key:
                            _disable_key(provider, key_index, typed_err.cooldown_seconds, typed_err.category)
                        if typed_err.exhaust_quota_scope:
                            _mark_quota_scope_exhausted(quota_scope, seconds=typed_err.cooldown_seconds, reason=typed_err.category)
                            break
                        if not typed_err.allow_fallback_provider:
                            abort_fallback_providers = True
                        if not typed_err.retry_another_key and (typed_err.category == "MODEL_UNSUPPORTED" or _is_groq_schema_400_error(msg)):
                            break
                        continue

                    err_cls = (
                        _agt_mod.classify_provider_error(msg, default_retry_seconds=GROQ_RETRY_SECONDS)
                        if _agt_mod is not None
                        else None
                    )
                    if err_cls is not None:
                        last_err_category = err_cls["category"]
                        if err_cls["disable_key"]:
                            _disable_key(provider, key_index, err_cls["cooldown_seconds"], err_cls["reason"])
                        if err_cls["exhaust_quota_scope"]:
                            _mark_quota_scope_exhausted(quota_scope, seconds=err_cls["cooldown_seconds"], reason=err_cls["category"])
                            break
                        continue

                    if _is_quota_error(msg):
                        last_err_category = "DAILY_QUOTA_EXHAUSTED"
                        _disable_key(provider, key_index, 24 * 60 * 60, "Quota harian")
                        _mark_quota_scope_exhausted(quota_scope, seconds=24 * 60 * 60, reason="DAILY_QUOTA_EXHAUSTED")
                        break
                    if _is_rate_limit_error(msg):
                        last_err_category = "RATE_LIMIT_TEMPORARY"
                        _disable_key(provider, key_index, GROQ_RETRY_SECONDS, "Rate limit")
                        continue
                    if _is_auth_error(msg):
                        last_err_category = "AUTH_ERROR"
                        _disable_key(provider, key_index, 24 * 60 * 60, "API key/balance")
                        continue
                    if _is_model_not_found(msg):
                        last_err_category = "MODEL_UNSUPPORTED"
                        continue
                    if "timeout" in msg.lower() or "connection" in msg.lower():
                        last_err_category = "TIMEOUT_OR_NETWORK"
                        _disable_key(provider, key_index, 60, "Network/timeout")
                        continue
                    continue

            continue

    raw_summary = " | ".join(errors[-5:]) if errors else "Tidak ada provider API yang tersedia."
    summary = redact_secrets(raw_summary, extra_secrets=list(GROQ_KEYS) + list(GEMINI_KEYS)) if _cfg_mod is not None else raw_summary
    status_code = (
        "BUDGET_EXHAUSTED"
        if (
            last_err_category == "DAILY_QUOTA_EXHAUSTED"
            or any("PHYSICAL_CAP_REACHED" in e or "DEADLINE_EXCEEDED" in e or "QUOTA_SCOPE_EXHAUSTED" in e for e in errors)
        )
        else "FAILED"
    )
    return {
        "status": status_code,
        "error_category": last_err_category or "UNKNOWN",
        "decision": "WAIT",
        "confidence": 0.0,
        "reasoning": f"AI gagal setelah mencoba provider yang tersedia: {summary}",
        "key_risk": "Periksa API key, quota, akses model, atau koneksi provider.",
        "evidence": [],
        "modes": {},
        "tier": tier,
        "provider": "none",
        "model": "",
        "finish_reason": None,
        "usage": _extract_usage(None),
        "requested_max_tokens": requested_tokens,
        "effective_max_tokens": effective_tokens,
    }

# -------------------------
# Market packet (PH04-01 Multi-Horizon 5m/15m/1h/4h/1d)
# -------------------------
def market_packet(symbol, as_of=None, use_cache=False, cache=None, budget=None, include_higher_timeframes=True):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    active_budget = budget
    frames = {}
    snapshots = {}
    timeframe_diagnostics = {}

    tf_specs = [("5m", 260), ("15m", 260), ("1h", 260)]
    if include_higher_timeframes:
        tf_specs.extend([("4h", 260), ("1d", 260)])

    for tf, limit in tf_specs:
        is_core_tf = tf in ("5m", "15m", "1h")
        snap = None
        raw_df = None
        try:
            if use_cache and active_cache is not None:
                snap = active_cache.get_kline_snapshot(symbol, tf, limit, now_ms=as_of)
            if snap is None:
                if active_budget is not None:
                    ok_b, reason_b = active_budget.check_and_reserve("/api/v3/klines", now_ms=as_of)
                    if not ok_b:
                        if is_core_tf:
                            raise RuntimeError(reason_b)
                        timeframe_diagnostics[tf] = {
                            "loaded": False,
                            "status": reason_b.split(":")[0],
                            "error": reason_b,
                            "ema200_ready": False,
                            "warmup_status": "MISSING",
                        }
                        continue
                raw_df = klines(symbol, tf, limit)
                if _mkt_mod is not None:
                    snap = _mkt_mod.build_timeframe_snapshot(raw_df, symbol=symbol, interval=tf, as_of=as_of, limit=limit)
                    if use_cache and active_cache is not None and snap.validation.valid:
                        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms)
            if snap is not None:
                df_closed = snap.copy_closed_df() if len(snap.closed_df) > 0 else snap.raw_df
                if raw_df is not None and hasattr(raw_df, "attrs"):
                    for ak in ("warmup_report", "ema200_ready", "warmup_status", "indicator_version"):
                        if ak in raw_df.attrs:
                            df_closed.attrs[ak] = raw_df.attrs[ak]
                if len(df_closed) == 0 or not snap.validation.valid:
                    if is_core_tf and len(df_closed) == 0:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframe_diagnostics[tf] = {
                        "loaded": False,
                        "status": "INVALID_OR_EMPTY_DATA",
                        "errors": list(snap.validation.errors),
                        "ema200_ready": False,
                        "warmup_status": "EMPTY_DATA" if len(df_closed) == 0 else "INVALID_DATA",
                    }
                    continue
                snapshots[tf] = snap
                frames[tf] = df_closed
            else:
                df_closed = _closed_df(raw_df, as_of=as_of, interval=tf)
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframe_diagnostics[tf] = {
                        "loaded": False,
                        "status": "EMPTY_DATA",
                        "ema200_ready": False,
                        "warmup_status": "EMPTY_DATA",
                    }
                    continue
                frames[tf] = df_closed
        except Exception as exc:
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc) and active_budget is not None:
                active_budget.record_rate_limit(_scan_mod.extract_retry_after_seconds(exc), now_ms=as_of)
            if is_core_tf:
                raise
            timeframe_diagnostics[tf] = {
                "loaded": False,
                "status": "FETCH_FAILED",
                "error": _safe_ui_error(exc)[:240],
                "ema200_ready": False,
                "warmup_status": "FETCH_FAILED",
            }

    ob = None
    if use_cache and active_cache is not None:
        ob = active_cache.get_order_book(symbol, limit=20, now_ms=as_of)
    if ob is None:
        if active_budget is not None:
            ok_ob, reason_ob = active_budget.check_and_reserve("/api/v3/depth", now_ms=as_of)
            if not ok_ob:
                raise RuntimeError(reason_ob)
        ob = order_book(symbol)
        if use_cache and active_cache is not None and isinstance(ob, dict):
            active_cache.put_order_book(symbol, ob, limit=20, fetched_at_ms=as_of)
    d = frames["15m"]
    c = candle_info(d)
    a = d.iloc[-1]
    support = float(d.Low.tail(30).min())
    resistance = float(d.High.tail(30).max())

    smc_by_tf = {tf: smc_analysis(df, interval=tf) for tf, df in frames.items()}
    as_of_ms_val = snapshots["15m"].as_of_ms if "15m" in snapshots else (int(time.time() * 1000))
    as_of_iso_val = snapshots["15m"].as_of_iso if "15m" in snapshots else datetime.now(timezone.utc).isoformat()

    live_15m = snapshots["15m"].live_bar.to_dict() if ("15m" in snapshots and snapshots["15m"].live_bar is not None) else None
    quote_price = float(live_15m["close"]) if (live_15m is not None and live_15m.get("close", 0) > 0) else float(a.Close)

    packet = {
        "symbol": symbol,
        "live_market_packet": True,
        "as_of_ms": as_of_ms_val,
        "as_of": as_of_iso_val,
        "price": quote_price,
        "closed_price": float(a.Close),
        "market_regime": market_regime(d),
        "order_book": ob,
        "support": support,
        "resistance": resistance,
        "candlestick": c,
        "timeframes": {},
        "timeframe_diagnostics": timeframe_diagnostics,
        "higher_tf_errors": {
            tf: str(diag.get("error") or diag.get("status") or "FAILED")
            for tf, diag in timeframe_diagnostics.items()
            if not diag.get("loaded", False)
        },
        "watermarks": {tf: s.watermark.to_dict() for tf, s in snapshots.items()},
        "live_quotes": {tf: (s.live_bar.to_dict() if s.live_bar is not None else None) for tf, s in snapshots.items()},
        "validation": {tf: s.validation.to_dict() for tf, s in snapshots.items()},
        "freshness": {
            tf: {
                "freshness_seconds": s.freshness_seconds,
                "is_fresh": s.is_fresh,
                "status": s.freshness_status,
                "cache_key": s.cache_key,
                "from_cache": s.from_cache,
            }
            for tf, s in snapshots.items()
        },
        "smc": smc_by_tf["15m"],
        "smc_by_tf": smc_by_tf,
    }
    for tf, df in frames.items():
        x = df.iloc[-1]
        wm_info = packet["watermarks"].get(tf, {})
        w_rep = df.attrs.get("warmup_report") if isinstance(df.attrs.get("warmup_report"), dict) else {}
        raw_bar_count = int(w_rep.get("bar_count", len(df)))
        ema200_ready = bool(df.attrs.get("ema200_ready", raw_bar_count >= 200))
        warmup_status = str(df.attrs.get("warmup_status", "READY" if ema200_ready else "PARTIAL_WARMUP_NO_EMA200"))
        quality_status = str(w_rep.get("quality_status", "OK"))
        packet["timeframes"][tf] = {
            "price": float(x.Close),
            "ema20": float(x.EMA20),
            "ema50": float(x.EMA50),
            "ema200": float(x.EMA200),
            "rsi": float(x.RSI),
            "macd_hist": float(x.MACD_HIST),
            "atr": float(x.ATR),
            "vol_ratio": float(x.VOL_RATIO),
            "buy_pressure": float(x.BUY_PRESSURE),
            "bb_upper": float(x.BB_UP),
            "bb_lower": float(x.BB_LOW),
            "open_time": wm_info.get("open_time"),
            "close_time": wm_info.get("close_time"),
            "is_closed": wm_info.get("is_closed", True),
            "bar_count": raw_bar_count,
            "usable_bar_count": len(df),
            "ema200_ready": ema200_ready,
            "warmup_status": warmup_status,
            "quality_status": quality_status,
            "indicator_version": str(df.attrs.get("indicator_version", "v6.0-wilder-ewm-1")),
        }
        packet["timeframe_diagnostics"][tf] = {
            "loaded": True,
            "status": warmup_status,
            "bar_count": raw_bar_count,
            "usable_bar_count": len(df),
            "ema200_ready": ema200_ready,
            "warmup_status": warmup_status,
            "quality_status": quality_status,
            "from_cache": bool(packet["freshness"].get(tf, {}).get("from_cache", False)),
        }
    return frames, packet

def packet_text(packet):
    lines = [
        f"Symbol: {packet['symbol']}",
        f"Harga: ${packet['price']:.8f}",
        f"Market regime: {packet['market_regime']}",
        f"Support 30 candle: ${packet['support']:.8f}",
        f"Resistance 30 candle: ${packet['resistance']:.8f}",
        f"Order book imbalance: {packet['order_book']['imbalance']:+.2%}",
        f"Spread: {packet['order_book']['spread']:.3f}%",
        f"Candlestick: {packet['candlestick']['name']}",
        f"Body: {packet['candlestick']['body']:.1%}",
        f"Upper wick: {packet['candlestick']['upper_wick']:.1%}",
        f"Lower wick: {packet['candlestick']['lower_wick']:.1%}",
        "",
        "=== SMART MONEY / PRICE STRUCTURE ===",
        smc_text(packet["smc"]),
        "",
    ]
    for tf, x in packet["timeframes"].items():
        lines.extend([
            f"[TIMEFRAME {tf}]",
            f"WARMUP_STATUS={x.get('warmup_status', 'READY')} EMA200_READY={x.get('ema200_ready', True)}",
            f"EMA20={x['ema20']:.8f}",
            f"EMA50={x['ema50']:.8f}",
            f"EMA200={x['ema200']:.8f}",
            f"RSI={x['rsi']:.2f}",
            f"MACD_HIST={x['macd_hist']:.8f}",
            f"ATR={x['atr']:.8f}",
            f"VOL_RATIO={x['vol_ratio']:.2f}x",
            f"TAKER_BUY_PRESSURE={x['buy_pressure']:.1%}",
            f"BB_UP={x['bb_upper']:.8f}",
            f"BB_LOW={x['bb_lower']:.8f}",
            "",
            f"[SMC {tf}]",
            smc_text(packet["smc_by_tf"][tf]),
            "",
        ])
    return "\n".join(lines)

def specialist_data(packet, focus):
    tf = packet["timeframes"]
    smc = packet["smc"]
    avail_tfs = [name for name in ("5m", "15m", "1h", "4h", "1d") if name in packet.get("smc_by_tf", {}) and name in tf]
    lines = [
        f"Symbol: {packet['symbol']}",
        f"Harga: {packet['price']:.8f}",
        f"Regime: {packet['market_regime']}",
        f"Support: {packet['support']:.8f}",
        f"Resistance: {packet['resistance']:.8f}",
        f"Available Timeframes: {', '.join(avail_tfs)}",
    ]
    if focus == "structure":
        for name in avail_tfs:
            stc = packet["smc_by_tf"][name]["structure"]
            t_info = tf[name]
            lines += [
                f"[{name}] bias={stc['bias']} BOS={stc['bos']} CHOCH={stc['choch']} warmup={t_info.get('warmup_status', 'READY')}",
                f"[{name}] swing_high={stc['last_swing_high']:.8f} swing_low={stc['last_swing_low']:.8f}",
            ]
    elif focus == "liquidity":
        for name in avail_tfs:
            liq = packet["smc_by_tf"][name]["liquidity"]
            lines += [
                f"[{name}] sweep_high={liq['sweep_high']} sweep_low={liq['sweep_low']}",
                f"[{name}] equal_high={liq['equal_high']} equal_low={liq['equal_low']}",
            ]
    elif focus == "smc":
        lines.append("=== ORDER BLOCK / FVG ===")
        lines.append(smc_text(smc))
        for z in smc["order_blocks"][:8]:
            lines.append(
                f"OB id={z.get('zone_id', '-')} {z['direction']} {z['low']:.8f}..{z['high']:.8f} state={z.get('state', 'FRESH')} age={z['age']} vol={z['volume_ratio']:.2f}"
            )
        for z in smc["fvgs"][:8]:
            lines.append(
                f"FVG id={z.get('zone_id', '-')} {z['direction']} {z['low']:.8f}..{z['high']:.8f} state={z.get('state', 'FRESH')} age={z['age']} filled={z['filled']}"
            )
        for htf in ("1h", "4h", "1d"):
            if htf in packet.get("smc_by_tf", {}):
                h_smc = packet["smc_by_tf"][htf]
                lines.append(
                    f"[{htf} SMC] bias={h_smc['structure']['bias']} timing={h_smc['timing']} score={h_smc['smc_score']}"
                )
    else:
        ob = packet["order_book"]
        for name in avail_tfs:
            x = tf[name]
            lines += [
                f"[{name}] RSI={x['rsi']:.2f} VOL_RATIO={x['vol_ratio']:.2f}x BUY_PRESSURE={x['buy_pressure']:.1%} EMA200_READY={x.get('ema200_ready', True)}",
                f"[{name}] MACD_HIST={x['macd_hist']:.8f} EMA20={x['ema20']:.8f} EMA50={x['ema50']:.8f} EMA200={x['ema200']:.8f}",
            ]
        lines += [
            f"Order imbalance={ob['imbalance']:+.2%}",
            f"Spread={ob['spread']:.3f}%",
            f"15m entry timing={smc['timing']} SMC score={smc['smc_score']}",
        ]
    return "\n".join(lines)

def memory_context(symbol, limit=5):
    try:
        con = db()
        rows = con.execute(
            """
            SELECT final_decision, confidence, result, r_multiple, market_regime
            FROM signals
            WHERE symbol=? AND result IN ('TP','SL')
            ORDER BY id DESC LIMIT ?
            """,
            (symbol, int(limit)),
        ).fetchall()
        con.close()
    except Exception:
        return "Tidak ada memory historis yang tersedia."
    if not rows:
        return "Belum ada paper-trade historis tertutup untuk simbol ini."
    return "\n".join(
        f"{i+1}. {r['final_decision']} C={float(r['confidence'] or 0):.0f} "
        f"hasil={r['result']} R={float(r['r_multiple'] or 0):+.1f} regime={r['market_regime']}"
        for i, r in enumerate(rows)
    )

# -------------------------
# V5.3 / V6 Hierarchical orchestration (PH04-02 Neutral Framing & Multi-Round Debate)
# -------------------------
ANALYSTS = list(NEUTRAL_ANALYSTS) if _agt_mod is not None else [
    ("🏗️ Structure Analyst", "Fokus market structure multi-timeframe: HH/HL/LH/LL, BOS, CHOCH, swing highs/lows, trend vs range."),
    ("💧 Liquidity Analyst", "Fokus liquidity pool, equal high/low, liquidity sweep, stop-run, false breakout, dan lokasi likuiditas."),
    ("🧱 Order Block & FVG Analyst", "Fokus Order Block, Fair Value Gap, displacement, freshness, confluence, kualitas zona, dan lokasi entry."),
    ("⚡ Execution Analyst", "Fokus volume, taker-buy pressure, order-book imbalance, ATR, RSI, EMA/MACD sebagai konfirmasi timing."),
]


def analyst_summary_text(items):
    lines = []
    for x in items:
        lines.append(
            f"{x['agent']} | {x['decision']} | C={x['confidence']:.0f} | "
            f"Bukti={'; '.join(x.get('evidence', []))} | Risiko={x['key_risk']}"
        )
    return "\n".join(lines)


def debate_summary_text(bull, bear):
    return (
        f"BULL: {bull['decision']} C={bull['confidence']:.0f}; "
        f"{bull['reasoning']}; bukti={'; '.join(bull.get('evidence', []))}; risiko={bull['key_risk']}\n"
        f"BEAR: {bear['decision']} C={bear['confidence']:.0f}; "
        f"{bear['reasoning']}; bukti={'; '.join(bear.get('evidence', []))}; risiko={bear['key_risk']}"
    )


def risk_summary_text(items):
    return "\n".join(
        f"{x['agent']} | {x['decision']} | C={x['confidence']:.0f} | "
        f"{x['reasoning']} | risiko={x['key_risk']}"
        for x in items
    )


def preflight_market_check(packet):
    """Deterministic preflight validation before spending AI tokens (PH01-03 / F38)."""
    veto_codes = []
    if not isinstance(packet, dict):
        return {"passed": False, "veto_codes": ["INSUFFICIENT_DATA"], "reason": "Packet pasar kosong/bukan dict."}

    tfs = packet.get("timeframes")
    smcs = packet.get("smc_by_tf")
    if not isinstance(tfs, dict) or not isinstance(smcs, dict):
        veto_codes.append("INSUFFICIENT_DATA")
    else:
        for req_tf in ("5m", "15m", "1h"):
            if req_tf not in tfs or req_tf not in smcs:
                veto_codes.append("INSUFFICIENT_DATA")
                break

    price = packet.get("price")
    support = packet.get("support")
    resistance = packet.get("resistance")
    ob = packet.get("order_book") if isinstance(packet.get("order_book"), dict) else {}
    spread = ob.get("spread", 0.0)
    tf15 = tfs.get("15m", {}) if isinstance(tfs, dict) else {}
    atr15 = tf15.get("atr")
    rsi15 = tf15.get("rsi")

    for val in (price, support, resistance, spread, atr15, rsi15):
        if not _is_finite_number(val):
            if "INVALID_NUMERIC" not in veto_codes:
                veto_codes.append("INVALID_NUMERIC")

    if "INVALID_NUMERIC" not in veto_codes:
        if float(price) <= 0 or float(atr15) <= 0 or float(support) <= 0 or float(resistance) <= 0 or float(support) > float(resistance):
            veto_codes.append("NON_POSITIVE_PRICE")
        if float(spread) < 0:
            veto_codes.append("INVALID_NUMERIC")
        elif float(spread) > float(MAX_SPREAD_PCT):
            veto_codes.append("SPREAD_TOO_WIDE")

    return {
        "passed": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "reason": "OK" if not veto_codes else f"Preflight veto: {', '.join(veto_codes)}",
    }


def _compute_candidate_trade_levels(packet, direction):
    """Calculate candidate entry, SL, TP, gross RR, and net RR after spread/fees for a proposed direction."""
    if direction not in {"LONG", "SHORT"}:
        return None, None, None, 0.0, 0.0

    current = packet.get("price")
    tf15 = packet.get("timeframes", {}).get("15m", {})
    atr15 = tf15.get("atr")
    if not _is_finite_number(current) or not _is_finite_number(atr15) or float(current) <= 0 or float(atr15) <= 0:
        return None, None, None, 0.0, 0.0

    current = float(current)
    atr15 = float(atr15)
    smc = packet.get("smc", {})
    spread = max(0.0, float(packet.get("order_book", {}).get("spread", 0.0) or 0.0))
    cost_abs = current * ((spread + max(0.0, float(ESTIMATED_ROUND_TRIP_COST_PCT))) / 100.0)
    min_risk = max(0.55 * atr15, current * 0.0045, cost_abs * 4.5)
    max_risk = max(2.5 * atr15, current * 0.03, min_risk * 1.5)
    target_net_rr = max(float(MIN_NET_RR) + 0.30, 1.85)

    if direction == "LONG":
        zone = smc.get("bullish_entry", {})
        z_low = zone.get("zone_low")
        raw_anchor = (
            float(z_low) - 0.10 * atr15
            if (_is_finite_number(z_low) and 0 < float(z_low) < current)
            else current - 1.10 * atr15
        )
        raw_dist = max(current - raw_anchor, 0.35 * atr15)
        risk = min(max(raw_dist, min_risk), max_risk)
        sl = max(current * 0.05, current - risk)
        risk = current - sl
        reward = max(2.0 * risk, target_net_rr * (risk + cost_abs) + cost_abs)
        tp = current + reward
    else:
        zone = smc.get("bearish_entry", {})
        z_high = zone.get("zone_high")
        raw_anchor = (
            float(z_high) + 0.10 * atr15
            if (_is_finite_number(z_high) and float(z_high) > current)
            else current + 1.10 * atr15
        )
        raw_dist = max(raw_anchor - current, 0.35 * atr15)
        risk = min(max(raw_dist, min_risk), max_risk)
        sl = current + risk
        reward = max(2.0 * risk, target_net_rr * (risk + cost_abs) + cost_abs)
        if current - reward <= current * 0.05:
            reward = current * 0.50
        tp = max(current * 0.05, current - reward)

    gross_rr = round(abs(tp - current) / max(risk, 1e-12), 2)
    net_reward = abs(tp - current) - cost_abs
    net_risk = abs(current - sl) + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    return current, sl, tp, gross_rr, net_rr


def validate_trade_plan_gate(direction, entry, sl, tp, gross_rr=2.0, spread_pct=0.0):
    """Deterministic post-AI validation of entry/SL/TP ordering, finite positive numbers, and net R:R (F38)."""
    veto_codes = []
    if direction not in {"LONG", "SHORT"}:
        return {"valid": False, "veto_codes": [], "net_rr": 0.0}

    for v in (entry, sl, tp, gross_rr, spread_pct):
        if not _is_finite_number(v):
            veto_codes.append("INVALID_NUMERIC")
            return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    entry_f, sl_f, tp_f = float(entry), float(sl), float(tp)
    if entry_f <= 0 or sl_f <= 0 or tp_f <= 0:
        veto_codes.append("NON_POSITIVE_PRICE")
        veto_codes.append("INVALID_SL_TP")
        return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    if direction == "LONG" and not (sl_f < entry_f < tp_f):
        veto_codes.append("INVALID_SL_TP")
    elif direction == "SHORT" and not (tp_f < entry_f < sl_f):
        veto_codes.append("INVALID_SL_TP")

    cost_abs = entry_f * ((max(0.0, float(spread_pct)) + max(0.0, float(ESTIMATED_ROUND_TRIP_COST_PCT))) / 100.0)
    net_reward = abs(tp_f - entry_f) - cost_abs
    net_risk = abs(entry_f - sl_f) + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    if net_rr < float(MIN_NET_RR):
        veto_codes.append("INSUFFICIENT_NET_RR")

    return {
        "valid": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "net_rr": round(float(net_rr), 4),
    }


def _extract_risk_vetoes(risk_agents, proposed_direction):
    """Detect explicit risk vetoes from Risk Manager agents (F16 / F38 / T05)."""
    vetoes = []
    if not isinstance(risk_agents, list):
        return vetoes
    for r in risk_agents:
        if not isinstance(r, dict):
            continue
        if r.get("veto") is True or r.get("veto_code"):
            code = str(r.get("veto_code") or "RISK_VETO").strip().upper()
            if code not in vetoes:
                vetoes.append(code)
            continue
        agent_name = str(r.get("agent", ""))
        r_dec = str(r.get("decision", "WAIT")).upper().strip()
        r_conf = float(r.get("confidence", 0.0) or 0.0) if _is_finite_number(r.get("confidence", 0.0)) else 0.0
        if "Conservative" in agent_name and proposed_direction in {"LONG", "SHORT"}:
            if (proposed_direction == "LONG" and r_dec == "SHORT") or (proposed_direction == "SHORT" and r_dec == "LONG"):
                if r_conf >= 70.0 and "RISK_VETO" not in vetoes:
                    vetoes.append("RISK_VETO")
    return vetoes


def is_timeframe_warmed_up_check(tf_info):
    if _dom_mod is not None:
        return _dom_mod.is_timeframe_warmed_up(tf_info)
    if not isinstance(tf_info, dict):
        return False, "MISSING_TIMEFRAME"
    if tf_info.get("ema200_ready") is False:
        return False, "PARTIAL_WARMUP_NO_EMA200"
    ws = str(tf_info.get("warmup_status", "READY") or "READY").upper()
    return (ws == "READY"), ws


def evaluate_mode_decisions(packet, raw_modes, gated_final, global_veto_codes=None, base_confidence=0.0, proactive_mode=None):
    """Evaluate per-mode decisions separating directional_bias from entry_action and enforcing horizon/global/warmup gates (F16, F17, T05, T15)."""
    if _dom_mod is not None:
        return _dom_mod.evaluate_mode_decisions(
            packet,
            raw_modes,
            gated_final,
            global_veto_codes=global_veto_codes,
            base_confidence=base_confidence,
            proactive_mode=proactive_mode,
        )
    return {}


def _resolve_proactive_ai_decision(packet, analyst_list, bull, bear, research_manager, trader, risk_list, portfolio_node):
    """
    Synthesize a decisive LONG or SHORT decision when AI Proaktif mode is active:
    - If Final Judge already chose LONG or SHORT, keeps that direction.
    - If Final Judge chose WAIT, resolves the stronger directional edge from all council agents
      (Analysts, Bull vs Bear, Research Manager, Trader, Risk Team) + multi-timeframe market metrics.
    """
    raw_j_dec = str((portfolio_node or {}).get("decision", "WAIT")).upper().strip()
    raw_j_conf = float((portfolio_node or {}).get("confidence", 0.0) or 0.0) if _is_valid_finite_confidence((portfolio_node or {}).get("confidence", 0.0)) else 65.0
    if raw_j_dec in {"LONG", "SHORT"}:
        return raw_j_dec, max(raw_j_conf, 62.0), "Keputusan langsung Final Judge"

    long_pts = 0.0
    short_pts = 0.0

    def _vote(node, weight):
        nonlocal long_pts, short_pts
        if not isinstance(node, dict) or str(node.get("status", "OK")).upper() != "OK":
            return
        d = str(node.get("decision", "WAIT")).upper().strip()
        c = float(node.get("confidence", 60.0) or 60.0) if _is_valid_finite_confidence(node.get("confidence", 60.0)) else 60.0
        if d == "LONG":
            long_pts += weight * (0.5 + c / 100.0)
        elif d == "SHORT":
            short_pts += weight * (0.5 + c / 100.0)

    for an in (analyst_list or []):
        _vote(an, 12.0)
    _vote(research_manager, 18.0)
    _vote(trader, 16.0)
    for rk in (risk_list or []):
        _vote(rk, 10.0)

    # Compare Bull vs Bear researcher confidence directly
    bull_c = float((bull or {}).get("confidence", 50.0) or 50.0) if isinstance(bull, dict) and _is_valid_finite_confidence((bull or {}).get("confidence", 50.0)) else 50.0
    bear_c = float((bear or {}).get("confidence", 50.0) or 50.0) if isinstance(bear, dict) and _is_valid_finite_confidence((bear or {}).get("confidence", 50.0)) else 50.0
    if str((bull or {}).get("decision", "")).upper() == "LONG":
        long_pts += bull_c * 0.25
    else:
        long_pts += bull_c * 0.12
    if str((bear or {}).get("decision", "")).upper() == "SHORT":
        short_pts += bear_c * 0.25
    else:
        short_pts += bear_c * 0.12

    # Multi-timeframe market & SMC objective directional score
    tfs = (packet or {}).get("timeframes", {}) if isinstance(packet, dict) else {}
    smc_by_tf = (packet or {}).get("smc_by_tf", {}) if isinstance(packet, dict) else {}
    tf_weights = {"5m": 1.0, "15m": 1.5, "1h": 1.5, "4h": 1.2, "1d": 1.0}
    for tf_k, w_tf in tf_weights.items():
        tf_d = tfs.get(tf_k, {}) if isinstance(tfs.get(tf_k), dict) else {}
        smc_d = smc_by_tf.get(tf_k, {}) if isinstance(smc_by_tf.get(tf_k), dict) else {}
        st_b = smc_d.get("structure", {}).get("bias", "RANGE")
        if st_b == "BULLISH":
            long_pts += 10.0 * w_tf
        elif st_b == "BEARISH":
            short_pts += 10.0 * w_tf

        d_scores = smc_d.get("directional_scores", {}) if isinstance(smc_d.get("directional_scores"), dict) else {}
        long_pts += float(d_scores.get("LONG", 0) or 0) * 0.12 * w_tf
        short_pts += float(d_scores.get("SHORT", 0) or 0) * 0.12 * w_tf

        p_v = tf_d.get("price")
        e20 = tf_d.get("ema20")
        e50 = tf_d.get("ema50")
        rsi_v = tf_d.get("rsi")
        macd_v = tf_d.get("macd_hist")
        bp_v = tf_d.get("buy_pressure")
        if _is_finite_number(p_v) and _is_finite_number(e20):
            if float(p_v) >= float(e20):
                long_pts += 6.0 * w_tf
            else:
                short_pts += 6.0 * w_tf
        if _is_finite_number(e20) and _is_finite_number(e50):
            if float(e20) >= float(e50):
                long_pts += 6.0 * w_tf
            else:
                short_pts += 6.0 * w_tf
        if _is_finite_number(rsi_v):
            if float(rsi_v) >= 50.0:
                long_pts += 4.0 * w_tf
            else:
                short_pts += 4.0 * w_tf
        if _is_finite_number(macd_v):
            if float(macd_v) >= 0.0:
                long_pts += 4.0 * w_tf
            else:
                short_pts += 4.0 * w_tf
        if _is_finite_number(bp_v):
            if float(bp_v) >= 0.50:
                long_pts += 4.0 * w_tf
            else:
                short_pts += 4.0 * w_tf

    imb = float((packet or {}).get("order_book", {}).get("imb", (packet or {}).get("order_book", {}).get("imbalance", 0.0)) or 0.0)
    if imb > 0.02:
        long_pts += 8.0
    elif imb < -0.02:
        short_pts += 8.0

    chosen_dir = "LONG" if long_pts >= short_pts else "SHORT"
    total_pts = max(long_pts + short_pts, 1.0)
    dominance = abs(long_pts - short_pts) / total_pts
    synth_conf = round(min(89.0, max(62.0, 62.0 + dominance * 35.0)), 1)
    summary_note = (
        f"Konsensus Proaktif AI memilih {chosen_dir} "
        f"(Skor Bull={long_pts:.1f} vs Bear={short_pts:.1f}, Confidence={synth_conf:.0f}/100)."
    )
    return chosen_dir, synth_conf, summary_note


def _extract_closed_bar_timestamp(frames_or_snap, tf="15m"):
    """Extract deterministic closed bar timestamp from frames/snapshot for idempotency and pattern fingerprinting (PH01-04 / F22, F24)."""
    if isinstance(frames_or_snap, dict):
        if frames_or_snap.get("bar_time"):
            return str(frames_or_snap["bar_time"])
        if frames_or_snap.get("created_at"):
            return str(frames_or_snap["created_at"])[:19]
        frames = frames_or_snap.get("frames") if isinstance(frames_or_snap.get("frames"), dict) else frames_or_snap
        if isinstance(frames, dict):
            df = frames.get(tf)
            if df is None and "5m" in frames:
                df = frames.get("5m")
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                closed = _closed_df(df)
                if len(closed) > 0:
                    return str(closed.index[-1])
                return str(df.index[-1])
        smc = frames_or_snap.get("smc") or frames_or_snap.get("packet", {}).get("smc")
        if isinstance(smc, dict):
            for z_list_key in ("order_blocks", "fvgs"):
                z_list = smc.get(z_list_key)
                if isinstance(z_list, list) and z_list and isinstance(z_list[0], dict) and z_list[0].get("created_at"):
                    return str(z_list[0]["created_at"])
        if frames_or_snap.get("analyzed_at"):
            return str(frames_or_snap["analyzed_at"])[:16]
    return "no_bar_time"


def compute_signal_idempotency_key(result, mode="scalping"):
    """Compute a durable setup idempotency key so manual UI and momentum monitor cannot save duplicate signals for the same bar/setup (PH01-04 / F24 / T16)."""
    if not isinstance(result, dict):
        return None
    if result.get("idempotency_key"):
        return str(result["idempotency_key"])
    symbol = str(result.get("symbol", "UNKNOWN")).upper().strip()
    final_dec = str(result.get("final", "WAIT")).upper().strip()
    mode_str = str(result.get("mode") or mode or "scalping").lower().strip()
    bar_ts = _extract_closed_bar_timestamp(result, tf="15m")
    entry_status = str(
        result.get("entry_status")
        or result.get("packet", {}).get("smc", {}).get("timing", "NONE")
    ).upper().strip()
    z_low = result.get("entry_zone_low")
    z_high = result.get("entry_zone_high")
    z_low_s = f"{float(z_low):.8f}" if _is_finite_number(z_low) else "none"
    z_high_s = f"{float(z_high):.8f}" if _is_finite_number(z_high) else "none"
    return f"sig|{symbol}|{mode_str}|{final_dec}|{bar_ts}|{entry_status}|{z_low_s}|{z_high_s}"


def run_multi_agent(
    symbol,
    scanner_score_value,
    debate_rounds=None,
    resume_analysis_id=None,
    max_parallel_workers=1,
    fail_after_node=None,
):
    is_resuming = bool(resume_analysis_id and str(resume_analysis_id).strip())
    analysis_id = (
        str(resume_analysis_id).strip()
        if is_resuming
        else f"ana_{symbol}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    )
    analyzed_at = datetime.now(timezone.utc).isoformat()
    candidate_deadline = time.time() + float(AI_CANDIDATE_DEADLINE_SECONDS)
    num_debate_rounds = max(1, min(2, int(debate_rounds if debate_rounds is not None else DEFAULT_DEBATE_ROUNDS)))
    physical_cap = _resolve_physical_cap()

    existing_ckpts = (
        _stg_mod.load_dag_checkpoints(analysis_id, db_file=DB_FILE)
        if (_stg_mod is not None and is_resuming)
        else {}
    )
    resumed_ai_nodes_count = sum(
        1
        for nid, cinfo in existing_ckpts.items()
        if nid not in {"preflight_gate", "post_ai_gate"}
        and str(cinfo.get("status", "")).upper() in {"COMPLETED", "OK"}
    )

    st.session_state["_ai_calls_current_candidate"] = resumed_ai_nodes_count
    st.session_state["_ai_physical_attempts_current_candidate"] = resumed_ai_nodes_count
    st.session_state["_ai_candidate_deadline"] = candidate_deadline
    reset_quota_scope_budget(
        scope_id="groq_default",
        max_physical_attempts=physical_cap,
        deadline_epoch=candidate_deadline,
    )
    with _QUOTA_SCOPE_LOCK:
        if "groq_default" in _QUOTA_SCOPE_BUDGETS:
            _QUOTA_SCOPE_BUDGETS["groq_default"]["physical_used"] = resumed_ai_nodes_count
            _QUOTA_SCOPE_BUDGETS["groq_default"]["logical_used"] = resumed_ai_nodes_count

    frames, packet = market_packet(symbol)
    bar_time_15m = _extract_closed_bar_timestamp({"frames": frames, "packet": packet}, tf="15m")
    data = packet_text(packet)
    _extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    durable_mgr = GLOBAL_DURABLE_BUDGET_MANAGER if _stg_mod is not None else None

    def _invoke_ai(role_name, prompt_data, instructions_text, tier="analyst", budget=None):
        try:
            return call_ai(
                role_name,
                prompt_data,
                instructions_text,
                tier=tier,
                budget=budget,
                analysis_id=analysis_id,
            )
        except TypeError:
            return call_ai(
                role_name,
                prompt_data,
                instructions_text,
                tier=tier,
                budget=budget,
            )

    analyst_meta = {
        "analyst_structure": ("🏗️ Structure Analyst", "structure", ANALYSTS[0][1]),
        "analyst_liquidity": ("💧 Liquidity Analyst", "liquidity", ANALYSTS[1][1]),
        "analyst_smc": ("🧱 Order Block & FVG Analyst", "smc", ANALYSTS[2][1]),
        "analyst_execution": ("⚡ Execution Analyst", "execution", ANALYSTS[3][1]),
    }

    risk_meta = {
        "risk_aggressive": (
            "🟢 Aggressive Risk Manager",
            "Toleransi risiko lebih tinggi; evaluasi peluang LONG maupun SHORT tetapi tetap cek invalidation.",
        ),
        "risk_neutral": (
            "🟡 Neutral Risk Manager",
            "Seimbangkan peluang vs risiko secara netral dan validasi setup LONG/SHORT.",
        ),
        "risk_conservative": (
            "🔴 Conservative Risk Manager",
            "Prioritaskan perlindungan modal, spread, volatilitas, dan false breakout.",
        ),
    }

    def _run_preflight_node(_upstream):
        pf = preflight_market_check(packet)
        if not pf["passed"]:
            return {
                "passed": False,
                "stage": "PREFLIGHT",
                "veto_codes": list(pf["veto_codes"]),
                "reason": pf["reason"],
                "budget_exhausted": False,
            }
        if durable_mgr is not None:
            judge_budget = MODEL_TIERS.get("judge", {}).get("max_tokens", JUDGE_MAX_TOKENS)
            ok_pf, pf_reason, judge_res_id = durable_mgr.preflight_reserve_judge(
                analysis_id=analysis_id,
                scope_id="groq_default",
                judge_budget=judge_budget,
                max_physical_attempts=physical_cap,
                deadline_epoch=candidate_deadline,
                db_file=DB_FILE,
            )
            if not ok_pf:
                return {
                    "passed": False,
                    "stage": "PREFLIGHT_BUDGET",
                    "veto_codes": [pf_reason.split(":")[0]],
                    "reason": pf_reason,
                    "budget_exhausted": True,
                }
            return {
                "passed": True,
                "stage": "PREFLIGHT",
                "veto_codes": [],
                "reason": "OK",
                "judge_reservation_id": judge_res_id,
            }
        return {
            "passed": True,
            "stage": "PREFLIGHT",
            "veto_codes": [],
            "reason": "OK",
        }

    def _make_analyst_runner(nid):
        role_name, focus_key, rule_text = analyst_meta[nid]
        def _runner(_upstream):
            raw_an = _invoke_ai(
                role_name,
                specialist_data(packet, focus_key),
                rule_text + " Ambil keputusan tegas LONG atau SHORT sesuai keunggulan bukti pada fokusmu (hindari WAIT kecuali benar-benar 50:50).",
                tier="analyst",
            )
            safe_an = (
                _agt_mod.sanitize_agent_output_for_downstream(raw_an, extra_secrets=_extra_s)
                if _agt_mod is not None
                else raw_an
            )
            return {"agent": role_name, **safe_an}
        return _runner

    def _collect_analysts(upstream):
        return [upstream[nid] for nid in ("analyst_structure", "analyst_liquidity", "analyst_smc", "analyst_execution") if nid in upstream]

    def _run_bull_r1(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        inst = (
            _agt_mod.build_bull_instructions(round_num=1, total_rounds=num_debate_rounds)
            if _agt_mod is not None
            else "Bangun kasus LONG paling kuat secara netral."
        )
        raw_b = _invoke_ai("🐂 Bull Researcher", d_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_b, extra_secrets=_extra_s) if _agt_mod is not None else raw_b

    def _run_bear_r1(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bull_r1 = upstream["debate_bull_r1"]
        bear_r1_data = (
            d_data
            + "\n\nARGUMEN BULL (PUTARAN 1) UNTUK DIUJI SECARA KRITIS:\n"
            + compact_json(bull_r1, 1100)
        )
        inst = (
            _agt_mod.build_bear_instructions(round_num=1, total_rounds=num_debate_rounds)
            if _agt_mod is not None
            else "Serang thesis Bull secara spesifik dan bangun tesis SHORT mandiri."
        )
        raw_br = _invoke_ai("🐻 Bear Researcher", bear_r1_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_br, extra_secrets=_extra_s) if _agt_mod is not None else raw_br

    def _run_bull_r2(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bull_r1 = upstream["debate_bull_r1"]
        bear_r1 = upstream["debate_bear_r1"]
        bull_r2_data = (
            d_data
            + "\n\nARGUMEN BULL (PUTARAN 1):\n"
            + compact_json(bull_r1, 900)
            + "\n\nKRITIK & TESIS BEAR (PUTARAN 1) YANG HARUS DITANGGAPI:\n"
            + compact_json(bear_r1, 900)
        )
        inst = _agt_mod.build_bull_instructions(round_num=2, total_rounds=num_debate_rounds)
        raw_b2 = _invoke_ai("🐂 Bull Researcher (Rebuttal)", bull_r2_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_b2, extra_secrets=_extra_s) if _agt_mod is not None else raw_b2

    def _run_bear_r2(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bear_r1 = upstream["debate_bear_r1"]
        bull_r2 = upstream["debate_bull_r2"]
        bear_r2_data = (
            d_data
            + "\n\nTESIS BEAR (PUTARAN 1):\n"
            + compact_json(bear_r1, 900)
            + "\n\nREBUTTAL BULL (PUTARAN 2) YANG HARUS DITANGGAPI:\n"
            + compact_json(bull_r2, 900)
        )
        inst = _agt_mod.build_bear_instructions(round_num=2, total_rounds=num_debate_rounds)
        raw_br2 = _invoke_ai("🐻 Bear Researcher (Rebuttal)", bear_r2_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_br2, extra_secrets=_extra_s) if _agt_mod is not None else raw_br2

    def _reconstruct_debate(upstream):
        rounds_hist = [
            {
                "round": 1,
                "bull": upstream["debate_bull_r1"],
                "bear": upstream["debate_bear_r1"],
            }
        ]
        if num_debate_rounds >= 2 and "debate_bull_r2" in upstream and "debate_bear_r2" in upstream:
            rounds_hist.append({
                "round": 2,
                "bull": upstream["debate_bull_r2"],
                "bear": upstream["debate_bear_r2"],
            })
        transcript = (
            _agt_mod.format_debate_transcript(rounds_hist)
            if _agt_mod is not None
            else debate_summary_text(rounds_hist[-1]["bull"], rounds_hist[-1]["bear"])
        )
        return rounds_hist[-1]["bull"], rounds_hist[-1]["bear"], rounds_hist, transcript

    def _run_research_manager(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        r_input = (
            data
            + "\n\nANALYST SUMMARY:\n" + a_summary
            + "\n\nBULL vs BEAR:\n" + debate_txt
            + "\n\nMEMORY HISTORIS PAPER TRADE:\n" + memory_context(symbol)
        )
        raw_rm = _invoke_ai(
            "🧠 Research Manager",
            r_input,
            "Sintesis bukti terbaik secara tegas untuk memilih LONG atau SHORT berdasarkan probabilitas arah tertinggi (structure, liquidity, OB/FVG, EMA/RSI/MACD, dan Bull vs Bear). Hindari memilih WAIT selama ada keunggulan arah.",
            tier="research",
        )
        return _agt_mod.sanitize_agent_output_for_downstream(raw_rm, extra_secrets=_extra_s) if _agt_mod is not None else raw_rm

    def _run_trader(upstream):
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        rm = upstream["research_manager"]
        t_input = (
            f"Market: {packet['symbol']} price={packet['price']:.8f}, ATR15={packet['timeframes']['15m']['atr']:.8f}, "
            f"support={packet['support']:.8f}, resistance={packet['resistance']:.8f}, regime={packet['market_regime']}\n"
            f"SMC: {smc_text(packet['smc'])}\n"
            f"Research: {compact_json(rm)}\n"
            f"Bull/Bear: {debate_txt}"
        )
        raw_tr = _invoke_ai(
            "💹 Trader",
            t_input,
            "Susun rencana eksekusi paper-trade secara tegas (LONG atau SHORT) berdasarkan arah dominan Research Manager, struktur harga, momentum EMA/RSI, serta level support/resistance/OB/FVG. Tetapkan apakah skenario terbaik adalah Market/Momentum Entry atau Retest Entry, dan pilih LONG atau SHORT secara tegas.",
            tier="analyst",
            budget=TRADER_MAX_TOKENS,
        )
        return _agt_mod.sanitize_agent_output_for_downstream(raw_tr, extra_secrets=_extra_s) if _agt_mod is not None else raw_tr

    def _make_risk_runner(nid):
        r_name, r_rule = risk_meta[nid]
        def _runner(upstream):
            rm = upstream["research_manager"]
            tr = upstream["trader_plan"]
            r_input = (
                f"Symbol={symbol}; price={packet['price']:.8f}; regime={packet['market_regime']}; "
                f"spread={packet['order_book']['spread']:.3f}%; imbalance={packet['order_book']['imbalance']:+.2%}; "
                f"support={packet['support']:.8f}; resistance={packet['resistance']:.8f}; "
                f"SMC={smc_text(packet['smc'])}; "
                f"Research={compact_json(rm)}; Trader={compact_json(tr)}"
            )
            raw_rk = _invoke_ai(
                r_name,
                r_input,
                r_rule + " Pilih keputusan tegas LONG atau SHORT yang memiliki profil risk/reward paling terukur.",
                tier="analyst",
                budget=RISK_MAX_TOKENS,
            )
            safe_rk = (
                _agt_mod.sanitize_agent_output_for_downstream(raw_rk, extra_secrets=_extra_s)
                if _agt_mod is not None
                else raw_rk
            )
            return {"agent": r_name, **safe_rk}
        return _runner

    def _collect_risks(upstream):
        return [upstream[nid] for nid in ("risk_aggressive", "risk_neutral", "risk_conservative") if nid in upstream]

    def _run_final_judge(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        rm = upstream["research_manager"]
        tr = upstream["trader_plan"]
        r_summary = risk_summary_text(_collect_risks(upstream))
        j_input = (
            f"MARKET: {symbol} price={packet['price']:.8f} regime={packet['market_regime']} "
            f"support={packet['support']:.8f} resistance={packet['resistance']:.8f} "
            f"spread={packet['order_book']['spread']:.3f}% imbalance={packet['order_book']['imbalance']:+.2%}\n"
            f"SMC / ENTRY TIMING:\n{smc_text(packet['smc'])}\n"
            f"ANALYSTS:\n{a_summary}\n"
            f"BULL/BEAR:\n{debate_txt}\n"
            f"RESEARCH:\n{compact_json(rm)}\n"
            f"TRADER:\n{compact_json(tr)}\n"
            f"RISK:\n{r_summary}\n"
            f"MEMORY: {memory_context(symbol, 3)}"
        )
        return _invoke_ai(
            "👨‍⚖️ Final Judge",
            j_input,
            "Ambil keputusan final yang TEGAS antara LONG atau SHORT berdasarkan keunggulan probabilitas dari structure, liquidity, OB/FVG, momentum EMA/RSI/MACD, order flow, Bull vs Bear, research, trader plan, dan risk team. Jangan memilih WAIT selama terdapat kecenderungan arah bullish (pilih LONG) atau bearish (pilih SHORT). Selain keputusan utama, WAJIB isi modes secara tegas (LONG atau SHORT) untuk tiga gaya: scalping memakai 1H→15M→5M, intraday memakai 4H→1H→15M, dan swing memakai 1D→4H→1H (jika timeframe 4H/1D tidak tersedia atau belum warm-up, tetapkan decision mode tersebut menjadi WAIT).",
            tier="judge",
        )

    def _run_post_ai_gate(upstream):
        portfolio_node = upstream["final_judge"]
        analyst_list = _collect_analysts(upstream)
        risk_list = _collect_risks(upstream)
        is_proactive_mode = bool(
            packet.get("live_market_packet", False)
            and st.session_state.get("v6_proactive_ai_mode", True)
        )

        raw_model_dec = str(portfolio_node.get("decision", "WAIT")).upper().strip()
        if raw_model_dec not in _ALLOWED_DECISIONS:
            raw_model_dec = "WAIT"

        j_conf = (
            float(portfolio_node.get("confidence", 0.0) or 0.0)
            if _is_valid_finite_confidence(portfolio_node.get("confidence", 0.0))
            else 0.0
        )

        if is_proactive_mode and raw_model_dec == "WAIT":
            bull_node = upstream.get("debate_bull_r2") or upstream.get("debate_bull_r1") or {}
            bear_node = upstream.get("debate_bear_r2") or upstream.get("debate_bear_r1") or {}
            rm_node = upstream.get("research_manager") or {}
            tr_node = upstream.get("trader_plan") or {}
            synth_dir, synth_conf, synth_note = _resolve_proactive_ai_decision(
                packet, analyst_list, bull_node, bear_node, rm_node, tr_node, risk_list, portfolio_node
            )
            raw_model_dec = synth_dir
            j_conf = synth_conf
            portfolio_node["decision"] = synth_dir
            portfolio_node["confidence"] = synth_conf
            orig_reason = str(portfolio_node.get("reasoning", "") or "").strip()
            portfolio_node["reasoning"] = f"[{synth_note}] {orig_reason}"[:700]
            if portfolio_node.get("status", "OK") != "OK":
                portfolio_node["status"] = "OK"

        final_dec = raw_model_dec
        v_codes = []

        if portfolio_node.get("status", "OK") != "OK" and not is_proactive_mode:
            v_codes.append(str(portfolio_node.get("status", "INVALID_AI_OUTPUT")))

        timing_val = packet["smc"]["timing"]
        if is_proactive_mode and raw_model_dec in {"LONG", "SHORT"}:
            timing_val = "READY_LONG" if raw_model_dec == "LONG" else "READY_SHORT"
            packet["smc"]["timing"] = timing_val

        if not is_proactive_mode:
            if raw_model_dec in {"LONG", "SHORT"}:
                if timing_val == "LATE":
                    v_codes.append("LATE_ENTRY")
                elif timing_val == "WAIT_RETEST":
                    v_codes.append("WAIT_RETEST")
                elif timing_val == "READY_LONG" and raw_model_dec == "SHORT":
                    v_codes.append("TIMEFRAME_CONFLICT")
                elif timing_val == "READY_SHORT" and raw_model_dec == "LONG":
                    v_codes.append("TIMEFRAME_CONFLICT")

            analyst_dir = sum(
                1 if (x.get("status", "OK") == "OK" and x.get("decision") == "LONG")
                else -1 if (x.get("status", "OK") == "OK" and x.get("decision") == "SHORT")
                else 0
                for x in analyst_list
            )
            risk_dir = sum(
                1 if (x.get("status", "OK") == "OK" and x.get("decision") == "LONG")
                else -1 if (x.get("status", "OK") == "OK" and x.get("decision") == "SHORT")
                else 0
                for x in risk_list
            )

            if raw_model_dec in {"LONG", "SHORT"} and j_conf < float(MIN_JUDGE_CONFIDENCE):
                v_codes.append("LOW_CONFIDENCE")
            elif raw_model_dec in {"LONG", "SHORT"}:
                support_dir = analyst_dir + risk_dir
                if raw_model_dec == "LONG" and support_dir <= -3:
                    v_codes.append("ANALYST_RISK_OPPOSITION")
                elif raw_model_dec == "SHORT" and support_dir >= 3:
                    v_codes.append("ANALYST_RISK_OPPOSITION")

            for r_veto in _extract_risk_vetoes(risk_list, raw_model_dec):
                if r_veto not in v_codes:
                    v_codes.append(r_veto)

        c_entry, c_sl, c_tp, c_rr, c_net_rr = _compute_candidate_trade_levels(packet, raw_model_dec)
        tp_bundle = None
        if _risk_mod is not None:
            tp_bundle = _risk_mod.build_trade_plan_and_sizing(
                packet,
                raw_model_dec,
                equity=PAPER_INITIAL_EQUITY,
                risk_fraction=PAPER_RISK_FRACTION,
                fee_pct_round_trip=PAPER_FEE_PCT_ROUND_TRIP,
                slippage_pct_round_trip=PAPER_SLIPPAGE_PCT_ROUND_TRIP,
                max_position_exposure_pct=PAPER_MAX_POSITION_EXPOSURE_PCT,
                max_total_exposure_pct=PAPER_MAX_TOTAL_EXPOSURE_PCT,
                max_total_risk_fraction=PAPER_MAX_TOTAL_RISK_FRACTION,
                max_open_positions=PAPER_MAX_OPEN_POSITIONS,
                min_net_rr=MIN_NET_RR,
                instrument_mode=PAPER_INSTRUMENT_MODE,
            )
        if raw_model_dec in {"LONG", "SHORT"}:
            p_gate = validate_trade_plan_gate(
                raw_model_dec,
                c_entry,
                c_sl,
                c_tp,
                gross_rr=c_rr,
                spread_pct=packet.get("order_book", {}).get("spread", 0.0),
            )
            for pv in p_gate["veto_codes"]:
                if pv not in v_codes:
                    v_codes.append(pv)
            if tp_bundle is not None:
                for rv in tp_bundle.get("veto_codes", []):
                    if rv not in {"WAIT_DECISION"} and rv not in v_codes:
                        v_codes.append(rv)
                c_net_rr = tp_bundle["net_rr"]
            else:
                c_net_rr = p_gate["net_rr"]

        if v_codes:
            final_dec = "WAIT"

        if final_dec in {"LONG", "SHORT"}:
            entry_val, sl_val, tp_val, rr_val = c_entry, c_sl, c_tp, c_rr
            pos_size = tp_bundle["position_size"] if tp_bundle is not None else 0.0
            notional_val = tp_bundle["notional_value"] if tp_bundle is not None else 0.0
            risk_budget_val = tp_bundle["risk_budget"] if tp_bundle is not None else round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4)
            net_rr_val = round(float(c_net_rr), 4) if _is_finite_number(c_net_rr) else 0.0
            inst_dict = tp_bundle["instrument"] if tp_bundle is not None else {}
            sizing_dict = tp_bundle["sizing"] if tp_bundle is not None else {}
        else:
            entry_val, sl_val, tp_val, rr_val = None, None, None, 0.0
            pos_size = 0.0
            notional_val = 0.0
            risk_budget_val = round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4)
            net_rr_val = 0.0
            inst_dict = (
                _risk_mod.resolve_instrument_semantics("WAIT", instrument_mode=PAPER_INSTRUMENT_MODE).to_dict()
                if _risk_mod is not None
                else {}
            )
            sizing_dict = tp_bundle["sizing"] if tp_bundle is not None else {}

        v_summary = ", ".join(v_codes) if v_codes else "NONE"
        g_report = {
            "approved": final_dec in {"LONG", "SHORT"} and len(v_codes) == 0,
            "stage": "POST_AI",
            "raw_model_decision": raw_model_dec,
            "gated_decision": final_dec,
            "veto_codes": v_codes,
            "veto_summary": v_summary,
            "gross_rr": rr_val,
            "net_rr": round(float(c_net_rr), 4) if _is_finite_number(c_net_rr) else 0.0,
            "risk_budget": risk_budget_val,
            "position_size": pos_size,
            "notional_value": notional_val,
            "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
        }

        global_only_vetoes = [
            vc for vc in v_codes
            if vc not in {"WAIT_RETEST", "LATE_ENTRY", "TIMEFRAME_CONFLICT"}
        ]
        m_decisions = evaluate_mode_decisions(
            packet,
            portfolio_node.get("modes") or {},
            final_dec,
            global_veto_codes=global_only_vetoes,
            base_confidence=j_conf,
            proactive_mode=is_proactive_mode,
        )

        return {
            "status": "OK",
            "raw_model_decision": raw_model_dec,
            "final": final_dec,
            "veto_codes": v_codes,
            "veto_summary": v_summary,
            "gate_report": g_report,
            "modes": m_decisions,
            "entry": entry_val,
            "sl": sl_val,
            "tp": tp_val,
            "rr": rr_val,
            "gross_rr": rr_val,
            "net_rr": net_rr_val,
            "risk_budget": risk_budget_val,
            "position_size": pos_size,
            "notional_value": notional_val,
            "instrument": inst_dict,
            "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
            "trade_plan": sizing_dict,
            "timing": timing_val,
        }

    node_runners = {
        "preflight_gate": _run_preflight_node,
        "analyst_structure": _make_analyst_runner("analyst_structure"),
        "analyst_liquidity": _make_analyst_runner("analyst_liquidity"),
        "analyst_smc": _make_analyst_runner("analyst_smc"),
        "analyst_execution": _make_analyst_runner("analyst_execution"),
        "debate_bull_r1": _run_bull_r1,
        "debate_bear_r1": _run_bear_r1,
        "research_manager": _run_research_manager,
        "trader_plan": _run_trader,
        "risk_aggressive": _make_risk_runner("risk_aggressive"),
        "risk_neutral": _make_risk_runner("risk_neutral"),
        "risk_conservative": _make_risk_runner("risk_conservative"),
        "final_judge": _run_final_judge,
        "post_ai_gate": _run_post_ai_gate,
    }
    if num_debate_rounds >= 2:
        node_runners["debate_bull_r2"] = _run_bull_r2
        node_runners["debate_bear_r2"] = _run_bear_r2

    def _skip_node_factory(node_spec, reason_str):
        base_skip = {
            "status": "SKIPPED",
            "decision": "WAIT",
            "confidence": 0.0,
            "reasoning": f"Preflight gate menolak setup sebelum AI dipanggil: {reason_str}.",
            "key_risk": reason_str,
            "evidence": [],
            "modes": {},
            "tier": node_spec.tier if node_spec.tier in MODEL_TIERS else "analyst",
            "provider": "none",
            "model": "",
            "finish_reason": None,
            "usage": _extract_usage(None),
            "requested_max_tokens": 0,
            "effective_max_tokens": 0,
        }
        if node_spec.node_id in analyst_meta or node_spec.node_id in risk_meta:
            return {"agent": node_spec.role, **base_skip}
        return base_skip

    orchestrator = _agt_mod.AnalysisDAGOrchestrator(
        debate_rounds=num_debate_rounds,
        checkpoint_loader=(
            (lambda aid: _stg_mod.load_dag_checkpoints(aid, db_file=DB_FILE))
            if _stg_mod is not None
            else None
        ),
        checkpoint_saver=(
            (lambda **kw: _stg_mod.save_dag_checkpoint(db_file=DB_FILE, extra_secrets=_extra_s, **kw))
            if _stg_mod is not None
            else None
        ),
        max_workers=max_parallel_workers,
        fail_after_node=fail_after_node,
    )

    dag_exec = orchestrator.execute(
        analysis_id,
        node_runners,
        skip_factory=_skip_node_factory,
    )
    outputs = dag_exec["outputs"]
    dag_report = dag_exec["dag_report"]

    pf_out = outputs["preflight_gate"]
    if not pf_out.get("passed", False):
        veto_codes = list(pf_out.get("veto_codes") or [])
        veto_summary = ", ".join(veto_codes)
        is_budget_pf = bool(pf_out.get("budget_exhausted", False))
        stage_name = "PREFLIGHT_BUDGET" if is_budget_pf else "PREFLIGHT"
        status_label = "BUDGET_EXHAUSTED" if is_budget_pf else "SKIPPED"
        preflight_msg = (
            f"Preflight Final Judge reservation gagal: {pf_out.get('reason')}"
            if is_budget_pf
            else f"Preflight gate menolak setup sebelum AI dipanggil: {veto_summary}."
        )
        skipped_agent = {
            "status": status_label,
            "decision": "WAIT",
            "confidence": 0.0,
            "reasoning": preflight_msg,
            "key_risk": str(pf_out.get("reason") or veto_summary),
            "evidence": [],
            "modes": {},
            "tier": "analyst",
            "provider": "none",
            "model": "",
            "finish_reason": None,
            "usage": _extract_usage(None),
            "requested_max_tokens": 0,
            "effective_max_tokens": 0,
        }
        analyst_results = [{"agent": name, **copy.deepcopy(skipped_agent)} for name, _ in ANALYSTS]
        risk_agents = [
            {"agent": "🟢 Aggressive Risk Manager", **copy.deepcopy(skipped_agent)},
            {"agent": "🟡 Neutral Risk Manager", **copy.deepcopy(skipped_agent)},
            {"agent": "🔴 Conservative Risk Manager", **copy.deepcopy(skipped_agent)},
        ]
        portfolio = {
            **copy.deepcopy(skipped_agent),
            "tier": "judge",
            "reasoning": preflight_msg,
        }
        mode_decisions = (
            {}
            if is_budget_pf
            else evaluate_mode_decisions(
                packet,
                {},
                "WAIT",
                global_veto_codes=veto_codes,
                base_confidence=0.0,
            )
        )
        gate_report = {
            "approved": False,
            "stage": stage_name,
            "raw_model_decision": "WAIT",
            "gated_decision": "WAIT",
            "veto_codes": veto_codes,
            "veto_summary": pf_out.get("reason") if is_budget_pf else veto_summary,
            "net_rr": 0.0,
        }
        out_res = {
            "analysis_id": analysis_id,
            "analyzed_at": analyzed_at,
            "bar_time": bar_time_15m,
            "symbol": symbol,
            "frames": frames,
            "packet": packet,
            "analysts": analyst_results,
            "bull": copy.deepcopy(skipped_agent),
            "bear": copy.deepcopy(skipped_agent),
            "research_manager": copy.deepcopy(skipped_agent),
            "trader": copy.deepcopy(skipped_agent),
            "risk_agents": risk_agents,
            "portfolio": portfolio,
            "modes": mode_decisions,
            "raw_model_decision": "WAIT",
            "final": "WAIT",
            "gate_report": gate_report,
            "veto_reasons": pf_out.get("reason") if is_budget_pf else veto_summary,
            "accounting_mode": LEGACY_ACCOUNTING_MODE,
            "scanner_score": scanner_score_value,
            "entry": None,
            "sl": None,
            "tp": None,
            "rr": 0.0,
            "gross_rr": 0.0,
            "net_rr": 0.0,
            "risk_budget": round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4),
            "position_size": 0.0,
            "quantity": 0.0,
            "notional_value": 0.0,
            "instrument_type": "NO_POSITION",
            "entry_status": packet.get("smc", {}).get("timing", "WAIT_RETEST"),
            "entry_zone_low": None,
            "entry_zone_high": None,
            "smc_score": packet.get("smc", {}).get("smc_score", 0),
            "smc_bias": packet.get("smc", {}).get("structure", {}).get("bias", "RANGE"),
            "model_tiers": MODEL_TIERS,
            "dag_report": dag_report,
            "budget_summary": (
                {"preflight_rejected": True, "reason": pf_out.get("reason")}
                if is_budget_pf
                else {
                    "logical_calls_used": 0,
                    "physical_attempts_used": 0,
                    "physical_attempts_cap": physical_cap,
                    "min_completion_tokens": MIN_COMPLETION_TOKENS,
                }
            ),
        }
        out_res["idempotency_key"] = compute_signal_idempotency_key(out_res)
        return out_res

    analyst_results = _collect_analysts(outputs)
    bull, bear, debate_rounds_history, debate = _reconstruct_debate(outputs)
    research_manager = outputs["research_manager"]
    trader = outputs["trader_plan"]
    risk_agents = _collect_risks(outputs)
    portfolio = outputs["final_judge"]
    post_gate = outputs["post_ai_gate"]

    final = post_gate["final"]
    raw_model_decision = post_gate["raw_model_decision"]
    gate_report = post_gate["gate_report"]
    veto_summary = post_gate["veto_summary"]
    mode_decisions = post_gate["modes"]
    entry_val = post_gate["entry"]
    sl = post_gate["sl"]
    tp = post_gate["tp"]
    rr = post_gate["rr"]
    net_rr_val = post_gate["net_rr"]
    risk_budget_val = post_gate["risk_budget"]
    pos_size = post_gate["position_size"]
    notional_val = post_gate["notional_value"]
    inst_dict = post_gate["instrument"]
    sizing_dict = post_gate["trade_plan"]
    timing = post_gate["timing"]

    out_res = {
        "analysis_id": analysis_id,
        "analyzed_at": analyzed_at,
        "bar_time": bar_time_15m,
        "symbol": symbol,
        "frames": frames,
        "packet": packet,
        "analysts": analyst_results,
        "bull": bull,
        "bear": bear,
        "debate_rounds": debate_rounds_history,
        "debate_rounds_count": len(debate_rounds_history),
        "debate_transcript": debate,
        "research_manager": research_manager,
        "trader": trader,
        "risk_agents": risk_agents,
        "portfolio": portfolio,
        "modes": mode_decisions,
        "raw_model_decision": raw_model_decision,
        "final": final,
        "gate_report": gate_report,
        "veto_reasons": veto_summary,
        "accounting_mode": LEGACY_ACCOUNTING_MODE,
        "scanner_score": scanner_score_value,
        "entry": entry_val,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "gross_rr": rr,
        "net_rr": net_rr_val,
        "risk_budget": risk_budget_val,
        "position_size": pos_size,
        "quantity": pos_size,
        "notional_value": notional_val,
        "instrument": inst_dict,
        "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
        "trade_plan": sizing_dict,
        "entry_status": timing,
        "entry_zone_low": (
            packet["smc"]["bullish_entry"]["zone_low"] if final == "LONG"
            else packet["smc"]["bearish_entry"]["zone_low"] if final == "SHORT"
            else None
        ),
        "entry_zone_high": (
            packet["smc"]["bullish_entry"]["zone_high"] if final == "LONG"
            else packet["smc"]["bearish_entry"]["zone_high"] if final == "SHORT"
            else None
        ),
        "smc_score": packet["smc"]["smc_score"],
        "smc_bias": packet["smc"]["structure"]["bias"],
        "model_tiers": MODEL_TIERS,
        "dag_report": dag_report,
        "budget_summary": {
            "logical_calls_used": int(st.session_state.get("_ai_calls_current_candidate", 0) or 0),
            "physical_attempts_used": int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0),
            "physical_attempts_cap": _resolve_physical_cap(),
            "min_completion_tokens": MIN_COMPLETION_TOKENS,
            "resumed_nodes_count": len(dag_report.get("nodes_resumed") or []),
            "executed_nodes_count": len(dag_report.get("nodes_executed") or []),
        },
    }
    out_res["idempotency_key"] = compute_signal_idempotency_key(out_res)
    if _agt_mod is not None:
        cons_report = _agt_mod.compute_directional_consensus(out_res)
        out_res["consensus_report"] = cons_report
        out_res["score_consensus"] = cons_report["score_consensus"]
    else:
        out_res["score_consensus"] = compute_consensus_score(out_res) if _stg_mod is not None else None
    out_res["workflow_status"] = (
        _agt_mod.build_workflow_stage_status(out_res, fingerprint=out_res["idempotency_key"])
        if _agt_mod is not None
        else {}
    )
    out_res["strategy_version"] = STRATEGY_VERSION if _cfg_mod is not None else "v5.6-ph02"
    out_res["prompt_version"] = PROMPT_VERSION if _cfg_mod is not None else "v5.6-id-strict-1"
    out_res["schema_version"] = SCHEMA_VERSION_AI if _cfg_mod is not None else "v5.6-json-1"
    out_res["model_config_hash"] = compute_model_config_hash(MODEL_TIERS) if _cfg_mod is not None else ""
    out_res["origin"] = "v6_runtime"
    out_res["accounting_quality"] = "legacy_r_snapshot"
    return out_res

# -------------------------
# Memory / paper trading
# -------------------------
def _clone_analysis_snapshot(result):
    """Create an immutable-style snapshot copy of an analysis result while keeping DataFrame frames intact."""
    snap = {}
    for k, v in result.items():
        if k == "frames":
            snap[k] = dict(v) if isinstance(v, dict) else v
        else:
            snap[k] = copy.deepcopy(v)
    if not snap.get("analysis_id"):
        snap["analysis_id"] = f"ana_{snap.get('symbol', 'UNKNOWN')}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if not snap.get("analyzed_at"):
        snap["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    if not snap.get("idempotency_key"):
        snap["idempotency_key"] = compute_signal_idempotency_key(snap)
    return snap


def ensure_analysis_state(state=None):
    """Initialize session_state keys required for decoupled analysis and save."""
    target = state if state is not None else st.session_state
    if "scan" not in target:
        target["scan"] = []
    if "last_result" not in target:
        target["last_result"] = None
    if "analyses_by_id" not in target:
        target["analyses_by_id"] = {}
    if "candidate_analysis_ids" not in target:
        target["candidate_analysis_ids"] = {}
    if "saved_analysis_ids" not in target:
        target["saved_analysis_ids"] = {}
    if "notified_analysis_ids" not in target:
        target["notified_analysis_ids"] = set()
    if "v56_processed_fingerprints" not in target:
        target["v56_processed_fingerprints"] = set()
    return target


def store_analysis_result(result, state=None):
    """Store an analysis result under its unique analysis_id and bind it to its symbol."""
    target = ensure_analysis_state(state)
    snap = _clone_analysis_snapshot(result)
    aid = snap["analysis_id"]
    target["analyses_by_id"][aid] = snap
    target["candidate_analysis_ids"][snap["symbol"]] = aid
    target["last_result"] = snap
    return snap


def get_rendered_analysis(symbol=None, state=None, analysis_id=None):
    """Pure lookup of stored analysis for rendering/saving; never triggers AI calls."""
    target = ensure_analysis_state(state)
    aid = analysis_id
    if not aid and symbol:
        aid = target["candidate_analysis_ids"].get(symbol)
    if not aid:
        return None
    return target["analyses_by_id"].get(aid)


def run_and_store_candidate_analysis(
    symbol,
    scanner_score_value,
    state=None,
    runner=None,
    resume_analysis_id=None,
    debate_rounds=None,
    max_parallel_workers=1,
    fail_after_node=None,
):
    """Explicit command to execute multi-agent analysis once (or resume from DAG checkpoint) and store its immutable snapshot."""
    target = ensure_analysis_state(state)
    fn = runner or run_multi_agent
    if fn is run_multi_agent or resume_analysis_id is not None or debate_rounds is not None or max_parallel_workers != 1 or fail_after_node is not None:
        try:
            raw_result = fn(
                symbol,
                scanner_score_value,
                debate_rounds=debate_rounds,
                resume_analysis_id=resume_analysis_id,
                max_parallel_workers=max_parallel_workers,
                fail_after_node=fail_after_node,
            )
        except TypeError:
            raw_result = fn(symbol, scanner_score_value)
    else:
        raw_result = fn(symbol, scanner_score_value)
    return store_analysis_result(raw_result, state=target)


def analyze_candidates_batch(candidates, top_n, state=None, force_refresh=False, runner=None):
    """Explicit command to analyze up to top_n candidates when requested by user action."""
    target = ensure_analysis_state(state)
    results = []
    for candidate in (candidates or [])[: max(int(top_n), 0)]:
        symbol = candidate["symbol"]
        existing = get_rendered_analysis(symbol=symbol, state=target)
        if existing is not None and not force_refresh:
            results.append(existing)
            continue
        res = run_and_store_candidate_analysis(
            symbol,
            candidate["score"],
            state=target,
            runner=runner,
        )
        results.append(res)
    return results


def _resolve_veto_reasons_str(result):
    if isinstance(result.get("veto_reasons"), str) and result["veto_reasons"].strip():
        return result["veto_reasons"].strip()
    gr = result.get("gate_report")
    if isinstance(gr, dict):
        codes = gr.get("veto_codes")
        if isinstance(codes, list) and codes:
            return ", ".join(str(c) for c in codes)
        if isinstance(gr.get("veto_summary"), str) and gr["veto_summary"].strip():
            return gr["veto_summary"].strip()
    return "NONE"


def _find_existing_signal_id(con, aid, ikey):
    if aid:
        row = con.execute("SELECT id FROM signals WHERE analysis_id=? LIMIT 1", (aid,)).fetchone()
        if row:
            return int(row["id"])
    if ikey:
        row = con.execute("SELECT id FROM signals WHERE idempotency_key=? LIMIT 1", (ikey,)).fetchone()
        if row:
            return int(row["id"])
    return None


def save_signal(result, analysis_id=None, idempotency_key=None):
    if _stg_mod is not None:
        return _stg_mod.save_signal_record(
            result,
            db_file=DB_FILE,
            analysis_id=analysis_id,
            idempotency_key=idempotency_key,
            extra_secrets=list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else []),
        )
    return 0


def save_rendered_analysis(analysis_id, state=None, tg_enabled=False):
    """Save a previously rendered analysis by its exact analysis_id or setup idempotency_key without re-running AI."""
    target = ensure_analysis_state(state)
    stored = get_rendered_analysis(state=target, analysis_id=analysis_id)
    if stored is None:
        raise KeyError(f"Analysis ID tidak ditemukan di session state: {analysis_id}")

    ikey = stored.get("idempotency_key") or compute_signal_idempotency_key(stored)
    already_in_session = analysis_id in target["saved_analysis_ids"]
    con = db()
    existing_id = _find_existing_signal_id(con, analysis_id, ikey)
    con.close()
    already_in_db = existing_id is not None

    signal_id = save_signal(stored, analysis_id=analysis_id, idempotency_key=ikey)
    target["saved_analysis_ids"][analysis_id] = signal_id
    created_new = not (already_in_session or already_in_db)

    tg_sent = False
    tg_msg = ""
    outbox_status = "NOT_APPLICABLE"
    if (
        tg_enabled
        and stored.get("final") in {"LONG", "SHORT"}
        and analysis_id not in target["notified_analysis_ids"]
        and ikey not in target["notified_analysis_ids"]
    ):
        try:
            ok, msg = telegram(telegram_message(stored))
            tg_sent = bool(ok)
            tg_msg = _safe_ui_error(msg)
        except Exception as tg_exc:
            tg_sent = False
            tg_msg = _safe_ui_error(tg_exc)
        if tg_sent:
            outbox_status = "SENT"
            target["notified_analysis_ids"].add(analysis_id)
            if ikey:
                target["notified_analysis_ids"].add(ikey)
        else:
            outbox_status = "FAILED_RETRYABLE"

    return {
        "signal_id": signal_id,
        "analysis_id": analysis_id,
        "idempotency_key": ikey,
        "created_new": created_new,
        "tg_sent": tg_sent,
        "tg_msg": tg_msg,
        "persistence_status": "SAVED",
        "notification_status": outbox_status,
    }


def reset_all_trading_state(state=None, confirmed=False, backup_before_reset=True):
    """Scoped destructive reset of sandbox DB tables (signals, votes, memories, pattern_events) and session state (PH01-04 / F32 / T26)."""
    if not confirmed:
        raise PermissionError("Reset database memerlukan konfirmasi eksplisit (confirmed=True).")

    backup_path = None
    con = db()
    if backup_before_reset and DB_FILE != ":memory:":
        try:
            row_count = sum(
                con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                for tbl in ("signals", "agent_votes", "memories", "pattern_events")
            )
            if row_count > 0:
                backup_path = f"{DB_FILE}.reset_backup"
                bcon = sqlite3.connect(backup_path)
                con.backup(bcon)
                bcon.close()
        except Exception:
            backup_path = None

    con.executescript(
        """
        DELETE FROM paper_account_ledger;
        DELETE FROM paper_positions;
        DELETE FROM agent_votes;
        DELETE FROM memories;
        DELETE FROM signals;
        DELETE FROM pattern_events;
        DELETE FROM usage_events;
        DELETE FROM agent_runs;
        DELETE FROM dag_checkpoints;
        DELETE FROM analysis_runs;
        DELETE FROM budget_reservations;
        DELETE FROM quota_scopes;
        DELETE FROM monitor_runtime_state;
        """
    )
    con.commit()
    con.close()

    target = ensure_analysis_state(state)
    target["saved_analysis_ids"] = {}
    target["notified_analysis_ids"] = set()
    target["v56_processed_fingerprints"] = set()
    target["analyses_by_id"] = {}
    target["candidate_analysis_ids"] = {}
    target["last_result"] = None
    target["confirm_reset"] = False

    with _QUOTA_SCOPE_LOCK:
        _QUOTA_SCOPE_BUDGETS.clear()

    return {"reset": True, "backup_path": backup_path}

def current_price(symbol):
    data = bget("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def update_paper_trades(
    bars_by_symbol=None,
    use_klines=False,
    tie_breaker="SL_FIRST_CONSERVATIVE",
    max_wait_bars=None,
    max_hold_bars=None,
    return_detailed=False,
):
    if _stg_mod is not None:
        return _stg_mod.reconcile_paper_positions(
            db_file=DB_FILE,
            bars_by_symbol=bars_by_symbol,
            kline_fetcher=(lambda sym, tf, lim: klines(sym, tf, lim)) if use_klines else None,
            price_fetcher=lambda sym: current_price(sym),
            tie_breaker=tie_breaker,
            max_wait_bars=max_wait_bars,
            max_hold_bars=max_hold_bars,
            initial_equity=PAPER_INITIAL_EQUITY,
            return_detailed=return_detailed,
        )
    return []

def format_signal_history_status(row):
    """Format signal history status so WAIT observations are never displayed as OPEN positions (F33)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    p_state = str(row["position_state"] if ("position_state" in keys and row["position_state"]) else "").upper().strip()
    res = row["result"] if "result" in keys else None
    if p_state in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP", "CLOSED_INVALIDATED", "EXPIRED_UNFILLED", "CANCELLED"}:
        return f"{res} ({p_state})" if res and res != p_state else p_state
    if res:
        return str(res)
    if p_state == "PENDING_ENTRY":
        return "PENDING_ENTRY"
    dec = str(row["final_decision"] if "final_decision" in keys else "").upper().strip()
    if dec in {"LONG", "SHORT"}:
        return "OPEN (LEGACY PAPER)"
    return "OBSERVED (WAIT)"


def _fallback_performance_metrics():
    try:
        con = db()
        closed_sigs = con.execute(
            "SELECT * FROM signals WHERE result IN ('TP','SL','TIME_STOP') AND final_decision IN ('LONG','SHORT') ORDER BY id DESC"
        ).fetchall()
        open_count = con.execute(
            "SELECT COUNT(*) FROM signals WHERE result IS NULL AND final_decision IN ('LONG','SHORT')"
        ).fetchone()[0]
        wait_count = con.execute(
            "SELECT COUNT(*) FROM signals WHERE final_decision = 'WAIT'"
        ).fetchone()[0]
        con.close()
    except Exception:
        closed_sigs = []
        open_count = 0
        wait_count = 0

    wins = sum(1 for x in closed_sigs if x["result"] == "TP")
    losses = sum(1 for x in closed_sigs if x["result"] == "SL")
    pos_r = sum(max(float(x["r_multiple"] or 0.0), 0.0) for x in closed_sigs)
    neg_r = abs(sum(min(float(x["r_multiple"] or 0.0), 0.0) for x in closed_sigs))
    pf_val = round(pos_r / neg_r, 4) if neg_r > 0 else None
    pf_status = "MEASURED_R_RATIO" if neg_r > 0 else ("UNDEFINED_NO_LOSSES" if pos_r > 0 else ("NO_CLOSED_TRADES" if not closed_sigs else "ZERO_ACTIVITY"))
    avg_r_val = round(sum(float(x["r_multiple"] or 0.0) for x in closed_sigs) / len(closed_sigs), 4) if closed_sigs else 0.0
    wr_val = round(wins / len(closed_sigs) * 100.0, 2) if closed_sigs else 0.0

    return {
        "closed": len(closed_sigs),
        "open": int(open_count),
        "pending_entry": 0,
        "expired_unfilled": 0,
        "observed_wait": int(wait_count),
        "sample_size": len(closed_sigs),
        "wins": wins,
        "losses": losses,
        "win_rate": wr_val,
        "avg_r": avg_r_val,
        "avg_net_r": avg_r_val,
        "total_gross_pnl": 0.0,
        "total_cost": 0.0,
        "total_net_pnl": 0.0,
        "profit_factor": pf_val,
        "profit_factor_status": pf_status,
        "accounting_mode": LEGACY_ACCOUNTING_MODE,
        "v6_realistic": None,
        "legacy_snapshot": {
            "accounting_mode": LEGACY_ACCOUNTING_MODE,
            "accounting_quality": "legacy_snapshot_only",
            "closed": len(closed_sigs),
            "wins": wins,
            "losses": losses,
            "avg_r": avg_r_val,
        },
        "accounting_disclaimer": "Metrik R-multiple legacy (snapshot TP=+2R / SL=-1R) dipisahkan dari buku kas/equity bersih V6 (`v6_realistic`).",
        "confidence_disclaimer": "Confidence adalah skor internal model (0-100), bukan probabilitas menang terkalibrasi.",
        "rows": closed_sigs,
    }


def performance():
    if _stg_mod is not None and hasattr(_stg_mod, "compute_dual_performance_metrics"):
        try:
            res = _stg_mod.compute_dual_performance_metrics(db_file=DB_FILE, initial_equity=PAPER_INITIAL_EQUITY)
            if isinstance(res, dict) and "closed" in res:
                return res
        except Exception:
            pass
    return _fallback_performance_metrics()

# -------------------------
# Telegram
# -------------------------
def _safe_ui_error(err_or_msg):
    extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _svc_mod is not None:
        return _svc_mod.sanitize_ui_error(err_or_msg, extra_secrets=extra_s)
    if _cfg_mod is not None:
        return str(redact_secrets(str(err_or_msg or ""), extra_secrets=extra_s))
    return str(err_or_msg or "")


def telegram(message):
    extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _svc_mod is not None:
        try:
            return _svc_mod.send_telegram_notification(
                message,
                tg_token=TG_TOKEN,
                tg_chat=TG_CHAT,
                post_fn=requests.post,
                extra_secrets=extra_s,
            )
        except Exception as exc:
            return False, _safe_ui_error(exc)
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram belum dikonfigurasi."

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": _safe_ui_error(message)},
            timeout=12,
        )
        if r.status_code != 200:
            return False, _safe_ui_error((r.text or "")[:300])
        data = r.json()
        return bool(data.get("ok")), _safe_ui_error(data.get("description", "OK"))
    except Exception as exc:
        return False, _safe_ui_error(exc)

def telegram_message(result):
    final = result["final"]
    icon = "🚀" if final == "LONG" else "🩸"

    return f"""🚨 AI TRADING V5.5

{icon} {result['symbol']}
SIGNAL: {final}
CONFIDENCE: {result['portfolio']['confidence']:.0f}/100
SCANNER: {result['scanner_score']}/100

💰 Entry: ${result['entry']:,.8f}
🎯 TP: ${result['tp']:,.8f}
🛑 SL: ${result['sl']:,.8f}
⚖️ R:R: 1:{result['rr']:.1f}

🧠 Market: {result['packet']['market_regime']}
🏗️ Structure: {result['packet']['smc']['structure']['bias']} | BOS={result['packet']['smc']['structure']['bos']}
🧱 SMC Score: {result['packet']['smc']['smc_score']}/100
⏱️ Entry Timing: {result['entry_status']}
📚 Order Imbalance: {result['packet']['order_book']['imbalance']:+.1%}
🕯 Candle: {result['packet']['candlestick']['name']}

👨‍⚖️ Final Judge:
{result['portfolio']['reasoning']}

⚠️ Paper-analysis only. Tidak ada order otomatis."""

# -------------------------
# V5.6 Pattern & Momentum Engine (PH04-01 Multi-Horizon 5m/15m/1h/4h/1d)
# -------------------------
def _pattern_score(frames):
    """Fast deterministic radar across scalping (1h->15m->5m), intraday (4h->1h->15m), and swing (1d->4h->1h)."""
    s5 = frames["5m"]["smc"]
    s15 = frames["15m"]["smc"]
    s1h = frames["1h"]["smc"]
    s4h = frames.get("4h", {}).get("smc") if isinstance(frames.get("4h"), dict) else None
    s1d = frames.get("1d", {}).get("smc") if isinstance(frames.get("1d"), dict) else None

    def _tf_warmed(tf_name):
        tf_entry = frames.get(tf_name)
        if not isinstance(tf_entry, dict) or not isinstance(tf_entry.get("smc"), dict):
            return False
        if "ema200_ready" in tf_entry and tf_entry.get("ema200_ready") is False:
            return False
        ws = str(tf_entry.get("warmup_status", "READY") or "READY").upper()
        return ws == "READY"

    score_long = 0
    score_short = 0
    evidence_long = []
    evidence_short = []

    def add(direction, points, text):
        nonlocal score_long, score_short
        if direction == "LONG":
            score_long += points; evidence_long.append(text)
        elif direction == "SHORT":
            score_short += points; evidence_short.append(text)

    if s1h["structure"]["bias"] == "BULLISH": add("LONG", 15, "1H structure bullish")
    if s1h["structure"]["bias"] == "BEARISH": add("SHORT", 15, "1H structure bearish")
    if s15["structure"]["bias"] == "BULLISH": add("LONG", 15, "15M structure bullish")
    if s15["structure"]["bias"] == "BEARISH": add("SHORT", 15, "15M structure bearish")

    if s15["structure"]["bos"] == "BULLISH BOS": add("LONG", 12, "15M bullish BOS")
    if s15["structure"]["bos"] == "BEARISH BOS": add("SHORT", 12, "15M bearish BOS")
    if s5["structure"]["choch"] == "BULLISH CHOCH": add("LONG", 14, "5M bullish CHOCH")
    if s5["structure"]["choch"] == "BEARISH CHOCH": add("SHORT", 14, "5M bearish CHOCH")
    if s5["liquidity"].get("sweep_low"): add("LONG", 15, "5M sell-side liquidity sweep")
    if s5["liquidity"].get("sweep_high"): add("SHORT", 15, "5M buy-side liquidity sweep")
    if s5.get("bullish_trigger"): add("LONG", 18, "5M bullish trigger")
    if s5.get("bearish_trigger"): add("SHORT", 18, "5M bearish trigger")
    if s15["bullish_entry"]["confluence"]: add("LONG", 8, "15M OB + FVG confluence")
    if s15["bearish_entry"]["confluence"]: add("SHORT", 8, "15M OB + FVG confluence")
    if s5["displacement"]["bullish"]: add("LONG", 8, "5M bullish displacement")
    if s5["displacement"]["bearish"]: add("SHORT", 8, "5M bearish displacement")

    if s4h is not None and _tf_warmed("4h"):
        if s4h["structure"]["bias"] == "BULLISH": evidence_long.append("4H structure bullish")
        elif s4h["structure"]["bias"] == "BEARISH": evidence_short.append("4H structure bearish")
    if s1d is not None and _tf_warmed("1d"):
        if s1d["structure"]["bias"] == "BULLISH": evidence_long.append("1D structure bullish")
        elif s1d["structure"]["bias"] == "BEARISH": evidence_short.append("1D structure bearish")

    for tf_s, w_s in ((s5, 0.20), (s15, 0.25), (s1h, 0.20)):
        ds = tf_s.get("directional_scores") if isinstance(tf_s.get("directional_scores"), dict) else {}
        score_long += int(round(float(ds.get("LONG", 0) or 0) * w_s))
        score_short += int(round(float(ds.get("SHORT", 0) or 0) * w_s))

    long_score = min(100, score_long)
    short_score = min(100, score_short)
    if max(long_score, short_score) < 35:
        state = "QUIET"
        direction = "WAIT"
        score = max(long_score, short_score)
    else:
        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        has_tf_trigger = (
            (direction == "LONG" and (s5.get("bullish_trigger") or s15.get("bullish_trigger")))
            or (direction == "SHORT" and (s5.get("bearish_trigger") or s15.get("bearish_trigger")))
        )
        if score >= 58 and has_tf_trigger:
            state = "TRIGGERED"
        elif score >= 45:
            state = "DEVELOPING"
        else:
            state = "WATCH"

    def _mode_matrix_entry(req_tfs, b_tf, st_tf, tr_tf):
        missing = [t for t in req_tfs if not isinstance(frames.get(t), dict) or not isinstance(frames.get(t, {}).get("smc"), dict)]
        unwarmed = [t for t in req_tfs if t not in missing and not _tf_warmed(t)]
        ok = (len(missing) == 0 and len(unwarmed) == 0)
        b_smc = frames.get(b_tf, {}).get("smc") if (b_tf not in missing and b_tf not in unwarmed) else None
        st_smc = frames.get(st_tf, {}).get("smc") if (st_tf not in missing and st_tf not in unwarmed) else None
        tr_smc = frames.get(tr_tf, {}).get("smc") if (tr_tf not in missing and tr_tf not in unwarmed) else None
        return {
            "bias_tf": b_tf,
            "setup_tf": st_tf,
            "trigger_tf": tr_tf,
            "required_timeframes": list(req_tfs),
            "missing_timeframes": missing,
            "unwarmed_timeframes": unwarmed,
            "bias": b_smc["structure"]["bias"] if b_smc is not None else "INSUFFICIENT_DATA",
            "setup": st_smc["structure"]["bias"] if st_smc is not None else "INSUFFICIENT_DATA",
            "trigger": tr_smc["timing"] if (ok and tr_smc is not None) else "INSUFFICIENT_DATA",
            "data_status": "COMPLETE" if ok else "INSUFFICIENT_DATA",
        }

    scalp_matrix = _mode_matrix_entry(("5m", "15m", "1h"), "1h", "15m", "5m")
    intraday_matrix = _mode_matrix_entry(("15m", "1h", "4h"), "4h", "1h", "15m")
    swing_matrix = _mode_matrix_entry(("1h", "4h", "1d"), "1d", "4h", "1h")

    return {
        "state": state,
        "direction": direction,
        "score": int(score),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "evidence": (evidence_long if direction == "LONG" else evidence_short)[:6],
        "scalping": scalp_matrix,
        "intraday": intraday_matrix,
        "swing": swing_matrix,
    }


def pattern_snapshot(symbol, as_of=None, use_cache=True, cache=None, budget=None, include_higher_timeframes=True):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    active_budget = budget
    now_ms = normalize_as_of_ms(as_of) if _mkt_mod is not None else None
    frames = {}
    from_cache_flags = {}
    timeframes_meta = {}

    tf_specs = [("5m", 120), ("15m", 140), ("1h", 140)]
    if include_higher_timeframes:
        tf_specs = [("5m", 260), ("15m", 260), ("1h", 260), ("4h", 260), ("1d", 260)]

    for tf, limit in tf_specs:
        is_core_tf = tf in ("5m", "15m", "1h")
        snap = None
        raw_df = None
        try:
            if use_cache and active_cache is not None:
                snap = active_cache.get_kline_snapshot(symbol, tf, limit, now_ms=now_ms)
            if snap is None:
                if active_budget is not None:
                    ok_b, reason_b = active_budget.check_and_reserve("/api/v3/klines", now_ms=now_ms)
                    if not ok_b:
                        if is_core_tf:
                            raise RuntimeError(reason_b)
                        timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "BUDGET_BLOCKED"}
                        continue
                raw_df = klines(symbol, tf, limit)
                if _mkt_mod is not None:
                    snap = _mkt_mod.build_timeframe_snapshot(raw_df, symbol=symbol, interval=tf, as_of=now_ms, limit=limit)
                    if use_cache and active_cache is not None and snap.validation.valid:
                        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms)
            if snap is not None:
                df_closed = snap.copy_closed_df() if len(snap.closed_df) > 0 else snap.raw_df
                if raw_df is not None and hasattr(raw_df, "attrs"):
                    for ak in ("warmup_report", "ema200_ready", "warmup_status", "indicator_version"):
                        if ak in raw_df.attrs:
                            df_closed.attrs[ak] = raw_df.attrs[ak]
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "EMPTY_DATA"}
                    continue
                frames[tf] = df_closed
                from_cache_flags[tf] = bool(snap.from_cache)
            else:
                df_closed = _closed_df(raw_df, as_of=now_ms, interval=tf)
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "EMPTY_DATA"}
                    continue
                frames[tf] = df_closed
                from_cache_flags[tf] = False

            w_rep = frames[tf].attrs.get("warmup_report") if isinstance(frames[tf].attrs.get("warmup_report"), dict) else {}
            raw_bar_count = int(w_rep.get("bar_count", len(frames[tf])))
            ema200_ok = bool(frames[tf].attrs.get("ema200_ready", raw_bar_count >= 200)) if include_higher_timeframes else True
            w_status = str(frames[tf].attrs.get("warmup_status", "READY" if ema200_ok else "PARTIAL_WARMUP_NO_EMA200")) if include_higher_timeframes else "READY"
            timeframes_meta[tf] = {
                "loaded": True,
                "bar_count": raw_bar_count,
                "usable_bar_count": len(frames[tf]),
                "ema200_ready": ema200_ok,
                "warmup_status": w_status,
            }
        except Exception:
            if is_core_tf:
                raise
            timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "FETCH_FAILED"}

    if not include_higher_timeframes and use_cache and active_cache is not None:
        for htf in ("4h", "1d"):
            cached_htf = active_cache.get_kline_snapshot(symbol, htf, 260, now_ms=now_ms)
            if cached_htf is not None and len(cached_htf.closed_df) > 0:
                frames[htf] = cached_htf.copy_closed_df()
                from_cache_flags[htf] = True
                raw_cnt = len(frames[htf])
                ema_ok = bool(frames[htf].attrs.get("ema200_ready", raw_cnt >= 200))
                timeframes_meta[htf] = {
                    "loaded": True,
                    "bar_count": raw_cnt,
                    "usable_bar_count": raw_cnt,
                    "ema200_ready": ema_ok,
                    "warmup_status": "READY" if ema_ok else "PARTIAL_WARMUP_NO_EMA200",
                }

    smcs = {tf: smc_analysis(df, interval=tf) for tf, df in frames.items()}
    score_input = {
        tf: {
            "smc": smcs[tf],
            "ema200_ready": timeframes_meta.get(tf, {}).get("ema200_ready", True),
            "warmup_status": timeframes_meta.get(tf, {}).get("warmup_status", "READY"),
        }
        for tf in smcs
    }
    signal = _pattern_score(score_input)
    price = float(frames["5m"].iloc[-1].Close)
    bar_time = _extract_closed_bar_timestamp({"frames": frames}, tf="15m")
    return {
        "symbol": symbol,
        "frames": frames,
        "smc": smcs,
        "timeframes_meta": timeframes_meta,
        "pattern": signal,
        "price": price,
        "bar_time": bar_time,
        "from_cache": from_cache_flags,
    }


def momentum_candidates(n=8, as_of=None, use_cache=True, cache=None, budget=None, return_report=False, include_higher_timeframes=False):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    if budget is None and _scan_mod is not None and hasattr(globals().get("GLOBAL_REQUEST_BUDGET"), "reset_cycle"):
        GLOBAL_REQUEST_BUDGET.reset_cycle(max_requests_per_cycle=1000, max_weight_per_cycle=5000, preserve_cooldown=True)
    active_budget = budget if budget is not None else (GLOBAL_REQUEST_BUDGET if _scan_mod is not None else None)
    now_ms = normalize_as_of_ms(as_of) if _mkt_mod is not None else None

    failed_symbols = []
    rate_limited = False
    retry_after_sec = 0.0

    tickers = None
    if use_cache and active_cache is not None:
        tickers = active_cache.get_tickers_24h(now_ms=now_ms)
    if tickers is None:
        if active_budget is not None:
            ok_t, reason_t = active_budget.check_and_reserve("/api/v3/ticker/24hr", now_ms=now_ms)
            if not ok_t:
                diag = {
                    "target_scan_count": 0,
                    "succeeded_count": 0,
                    "failed_count": 1,
                    "failed_symbols": [{"symbol": "ALL_TICKERS", "stage": "ticker_24hr", "error_code": reason_t.split(":")[0], "message": reason_t}],
                    "cache_stats": active_cache.stats() if active_cache else {},
                    "budget_stats": active_budget.snapshot(now_ms=now_ms),
                    "rate_limited": "RATE_LIMIT" in reason_t,
                    "retry_after_seconds": active_budget.last_retry_after_seconds,
                }
                if hasattr(st, "session_state") and isinstance(st.session_state, dict):
                    st.session_state["last_momentum_diagnostics"] = diag
                return {"candidates": [], "diagnostics": diag} if return_report else []
        try:
            tickers = bget("/api/v3/ticker/24hr")
            if use_cache and active_cache is not None and isinstance(tickers, list):
                active_cache.put_tickers_24h(tickers, fetched_at_ms=now_ms)
        except Exception as exc:
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = _scan_mod.extract_retry_after_seconds(exc)
                if active_budget is not None:
                    active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            raise

    pool = []
    stable_like = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE"}
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT") or symbol[:-4] in stable_like:
            continue
        qvol = float(t.get("quoteVolume", 0) or 0)
        if qvol < 3_000_000:
            continue
        change = float(t.get("priceChangePercent", 0) or 0)
        activity = min(qvol / 20_000_000, 5) * 10 + min(abs(change), 10) * 2
        pool.append((symbol, qvol, change, activity))
    pool.sort(key=lambda x: x[3], reverse=True)

    selected = pool[:max(int(n), 1)]
    out = []
    for symbol, qvol, change, activity in selected:
        try:
            snap = pattern_snapshot(symbol, as_of=now_ms, use_cache=use_cache, cache=active_cache, budget=active_budget, include_higher_timeframes=include_higher_timeframes)
            snap["qvol"] = qvol
            snap["change"] = change
            snap["activity_score"] = round(activity, 2)
            out.append(snap)
        except Exception as exc:
            err_code = "HTTP_429_RATE_LIMIT" if (_scan_mod is not None and _scan_mod.is_http_429_error(exc)) else "PATTERN_SNAPSHOT_FAILED"
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = _scan_mod.extract_retry_after_seconds(exc)
                if active_budget is not None:
                    active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            elif "RATE_LIMIT_COOLDOWN" in str(exc):
                err_code = "RATE_LIMIT_COOLDOWN"
                rate_limited = True
            elif "BUDGET_EXCEEDED" in str(exc):
                err_code = "REQUEST_BUDGET_EXCEEDED"
            failed_symbols.append({
                "symbol": symbol,
                "stage": "pattern_snapshot",
                "error_code": err_code,
                "message": _safe_ui_error(exc)[:240],
            })

    _state_priority = {"TRIGGERED": 3, "DEVELOPING": 2, "WATCH": 1, "QUIET": 0}
    sorted_out = sorted(
        out,
        key=lambda x: (
            _state_priority.get(str(x.get("pattern", {}).get("state", "QUIET")).upper(), 0),
            float(x.get("pattern", {}).get("score", 0) or 0),
            float(x.get("activity_score", 0.0) or 0.0),
        ),
        reverse=True,
    )
    diag = {
        "total_tickers": len(tickers or []),
        "liquid_universe_count": len(pool),
        "target_scan_count": len(selected),
        "succeeded_count": len(sorted_out),
        "failed_count": len(failed_symbols),
        "failed_symbols": failed_symbols,
        "cache_stats": active_cache.stats() if active_cache is not None else {},
        "budget_stats": active_budget.snapshot(now_ms=now_ms) if active_budget is not None else {},
        "rate_limited": rate_limited or (active_budget.snapshot(now_ms=now_ms)["in_cooldown"] if active_budget is not None else False),
        "retry_after_seconds": retry_after_sec or (active_budget.last_retry_after_seconds if active_budget is not None else 0.0),
    }
    if hasattr(st, "session_state") and isinstance(st.session_state, dict):
        st.session_state["last_momentum_diagnostics"] = diag

    if return_report:
        return {"candidates": sorted_out, "diagnostics": diag}
    return sorted_out


def pattern_fingerprint(snap, mode=None):
    """Time- and mode-aware pattern fingerprint so new setups on later bars/dates are not blocked forever (PH01-04 / F22 / T26)."""
    p = snap.get("pattern", {})
    smc = snap.get("smc", {})
    s5 = smc.get("5m", {})
    s15 = smc.get("15m", {})
    mode_str = str(mode or snap.get("mode") or "scalping").lower().strip()
    bar_time = _extract_closed_bar_timestamp(snap, tf="15m")

    dir_str = str(p.get("direction", "WAIT")).upper().strip()
    entry_key = "bullish_entry" if dir_str == "LONG" else "bearish_entry"
    zone_info = s15.get(entry_key, {}) if isinstance(s15.get(entry_key), dict) else {}
    z_low = zone_info.get("zone_low")
    z_high = zone_info.get("zone_high")
    zone_str = (
        f"{float(z_low):.8f}..{float(z_high):.8f}"
        if _is_finite_number(z_low) and _is_finite_number(z_high)
        else "no_zone"
    )

    return "|".join([
        str(snap.get("symbol", "UNKNOWN")),
        mode_str,
        str(bar_time),
        str(p.get("state", "QUIET")),
        dir_str,
        str(s5.get("timing", "NONE")),
        str(s15.get("timing", "NONE")),
        str(s5.get("structure", {}).get("bos", "NONE")),
        str(s5.get("structure", {}).get("choch", "NONE")),
        zone_str,
    ])


def save_pattern_event(snap, mode="scalping", notified=0):
    p = snap["pattern"]
    fp = pattern_fingerprint(snap, mode=mode)
    if _stg_mod is not None:
        return _stg_mod.save_pattern_event_record(
            snap,
            fingerprint=fp,
            mode=mode,
            notified=notified,
            db_file=DB_FILE,
        )
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    row = con.execute(
        "SELECT id, notified FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
        (fp,),
    ).fetchone()
    if row:
        con.close()
        return int(row["id"]), bool(row["notified"])
    cur = con.execute(
        "INSERT INTO pattern_events(created_at,symbol,mode,state,direction,score,fingerprint,notified) VALUES(?,?,?,?,?,?,?,?)",
        (now, snap["symbol"], mode, p["state"], p["direction"], p["score"], fp, int(notified)),
    )
    con.commit()
    event_id = int(cur.lastrowid)
    con.close()
    return event_id, bool(notified)


def mark_pattern_notified(event_id):
    con = db()
    con.execute("UPDATE pattern_events SET notified=1 WHERE id=?", (event_id,))
    con.commit()
    con.close()


def process_monitor_candidate_workflow(snap, state=None, tg_enabled=False, runner=None):
    """
    Execute the 3-stage monitor workflow (validation -> persistence -> notification) with durable
    SQLite checkpoints so WAIT outcomes or Telegram failures never re-run paid AI (F23).
    """
    target = ensure_analysis_state(state)
    event_id, already_notified = save_pattern_event(snap)
    fp = pattern_fingerprint(snap)
    wf_db = (
        _stg_mod.get_pattern_event_workflow(event_id=event_id, db_file=DB_FILE)
        if _stg_mod is not None
        else {"validation_status": "UNVALIDATED", "persistence_status": "UNSAVED", "notification_status": "SENT" if already_notified else "NOT_APPLICABLE", "analysis_id": None}
    )

    val_status = wf_db.get("validation_status", "UNVALIDATED")
    pers_status = wf_db.get("persistence_status", "UNSAVED")
    notif_status = wf_db.get("notification_status", "NOT_APPLICABLE")
    aid = wf_db.get("analysis_id")

    if val_status == "VALIDATED_WAIT" or fp in target["v56_processed_fingerprints"] and val_status != "VALIDATED_ENTRY":
        target["v56_processed_fingerprints"].add(fp)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": "VALIDATED_WAIT",
            "persistence_status": pers_status,
            "notification_status": notif_status,
            "analysis_id": aid,
        }

    if val_status == "VALIDATED_ENTRY" and pers_status == "SAVED" and (notif_status == "SENT" or already_notified or not tg_enabled):
        target["v56_processed_fingerprints"].add(fp)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": "SENT" if already_notified else notif_status,
            "analysis_id": aid,
        }

    if val_status == "VALIDATED_ENTRY" and pers_status == "SAVED" and tg_enabled and notif_status != "SENT":
        stored = get_rendered_analysis(state=target, analysis_id=aid) if aid else None
        if stored is not None:
            ok_tg, msg_tg = telegram(telegram_message(stored))
        else:
            ok_tg, msg_tg = telegram(pattern_alert_message(snap))
        new_notif = "SENT" if ok_tg else "FAILED_RETRYABLE"
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                notification_status=new_notif,
                notified=1 if ok_tg else 0,
                db_file=DB_FILE,
            )
        elif ok_tg:
            mark_pattern_notified(event_id)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": new_notif,
            "analysis_id": aid,
            "tg_msg": msg_tg,
        }

    result = run_and_store_candidate_analysis(
        snap["symbol"],
        snap["pattern"]["score"],
        state=target,
        runner=runner,
    )
    aid = result["analysis_id"]
    target["v56_processed_fingerprints"].add(fp)
    gate_ok = bool(result.get("gate_report", {}).get("approved", False))

    if result["final"] in {"LONG", "SHORT"} and gate_ok:
        val_status = "VALIDATED_ENTRY"
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                validation_status=val_status,
                analysis_id=aid,
                db_file=DB_FILE,
            )
        save_out = save_rendered_analysis(
            aid,
            state=target,
            tg_enabled=tg_enabled and not already_notified,
        )
        pers_status = "SAVED"
        notif_status = "SENT" if save_out["tg_sent"] else ("FAILED_RETRYABLE" if (tg_enabled and not already_notified) else "NOT_APPLICABLE")
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                validation_status=val_status,
                persistence_status=pers_status,
                notification_status=notif_status,
                analysis_id=aid,
                notified=1 if save_out["tg_sent"] else 0,
                db_file=DB_FILE,
            )
        elif save_out["tg_sent"]:
            mark_pattern_notified(event_id)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": True,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": notif_status,
            "analysis_id": aid,
            "signal_id": save_out["signal_id"],
            "result": result,
        }

    val_status = "VALIDATED_WAIT"
    if _stg_mod is not None:
        _stg_mod.update_pattern_event_workflow(
            event_id=event_id,
            validation_status=val_status,
            persistence_status="UNSAVED",
            notification_status="NOT_APPLICABLE",
            analysis_id=aid,
            db_file=DB_FILE,
        )
    return {
        "event_id": event_id,
        "fingerprint": fp,
        "ai_called": True,
        "validation_status": val_status,
        "persistence_status": "UNSAVED",
        "notification_status": "NOT_APPLICABLE",
        "analysis_id": aid,
        "result": result,
    }


def pattern_alert_message(snap):
    """Format momentum radar alert message cleanly without malformed nested f-strings or global variable dependence (PH01-04 / PH04-01 / F31 / T26)."""
    p = snap.get("pattern", {})
    smc = snap.get("smc", {})
    s5 = smc.get("5m", {"timing": "-", "structure": {"bias": "-"}})
    s15 = smc.get("15m", {"timing": "-", "structure": {"bias": "-"}})
    s1h = smc.get("1h", {"timing": "-", "structure": {"bias": "-"}})
    s4h = smc.get("4h", {"timing": "INSUFFICIENT_DATA", "structure": {"bias": "INSUFFICIENT_DATA"}})
    s1d = smc.get("1d", {"timing": "INSUFFICIENT_DATA", "structure": {"bias": "INSUFFICIENT_DATA"}})
    raw_evidence = p.get("evidence") or []
    evidence_lines = "\n".join(f"• {item}" for item in raw_evidence) if raw_evidence else "• Tidak ada bukti tambahan"
    price_val = float(snap.get("price", 0.0) or 0.0)
    intra_status = p.get("intraday", {}).get("data_status", "COMPLETE") if isinstance(p.get("intraday"), dict) else "COMPLETE"
    swing_status = p.get("swing", {}).get("data_status", "COMPLETE") if isinstance(p.get("swing"), dict) else "COMPLETE"
    intra_str = (
        f"4H {s4h['structure']['bias']} → 1H {s1h['structure']['bias']} → 15M {s15.get('timing', s15['structure']['bias'])} [{intra_status}]"
        if "4h" in smc else f"1H {s1h['structure']['bias']} → 15M {s15['structure']['bias']}"
    )
    swing_str = (
        f"1D {s1d['structure']['bias']} → 4H {s4h['structure']['bias']} → 1H {s1h.get('timing', s1h['structure']['bias'])} [{swing_status}]"
        if ("1d" in smc and "4h" in smc) else f"1H {s1h['structure']['bias']}"
    )
    return (
        f"🚨 V5.6 MOMENTUM ALERT\n\n"
        f"{snap.get('symbol', '-')} — {p.get('state', '-')}\n"
        f"Direction: {p.get('direction', 'WAIT')}\n"
        f"Radar score: {p.get('score', 0)}/100\n"
        f"Price: ${price_val:.8f}\n\n"
        f"⚡ Scalping: 1H {s1h['structure']['bias']} → 15M {s15['structure']['bias']} → 5M {s5['timing']}\n"
        f"📊 Intraday: {intra_str}\n"
        f"🌊 Swing: {swing_str}\n\n"
        f"Evidence:\n{evidence_lines}\n\n"
        f"⚠️ Radar only — AI council masih harus memvalidasi sebelum paper trade."
    )


def mode_fallback(result):
    """Fallback & per-mode gate if Final Judge omits or provides raw mode objects (PH01-03 / F16 / T05)."""
    packet = result.get("packet", {})
    final = str(result.get("final", "WAIT")).upper().strip()
    portfolio = result.get("portfolio", {}) if isinstance(result.get("portfolio"), dict) else {}
    conf = portfolio.get("confidence", 0.0)
    global_vetoes = []
    if isinstance(result.get("gate_report"), dict):
        global_vetoes = list(result["gate_report"].get("veto_codes") or [])
    raw_modes = portfolio.get("modes") if isinstance(portfolio.get("modes"), dict) else {}
    return evaluate_mode_decisions(
        packet,
        raw_modes,
        final,
        global_veto_codes=global_vetoes,
        base_confidence=conf,
    )


# -------------------------
# PH07-01 Independent Background Worker / CLI Monitor & Runtime Status Honesty (F29 / M7)
# -------------------------
def get_runtime_monitor_status(
    auto_enabled=False,
    interval_seconds=45,
    worker_id="default_worker",
    now_ms=None,
):
    """Return honest, SQLite-backed monitor runtime status (worker vs session_only) (F29)."""
    if _mon_mod is not None:
        return _mon_mod.get_durable_monitor_runtime_status(
            db_file=DB_FILE,
            auto_enabled=auto_enabled,
            interval_seconds=interval_seconds,
            worker_id=worker_id,
            now_ms=now_ms,
        )
    if _mkt_mod is not None:
        return _mkt_mod.get_monitor_runtime_status(
            execution_mode="session_only",
            auto_enabled=auto_enabled,
            interval_seconds=interval_seconds,
            now_ms=now_ms,
        )
    return {
        "execution_mode": "session_only",
        "runtime_label": "SESSION_ONLY_FRAGMENT" if auto_enabled else "MANUAL_ONLY",
        "worker_alive": False,
        "auto_fragment_enabled": bool(auto_enabled),
        "disclaimer": "Mode Session-Only (Streamlit UI): pemantauan otomatis hanya berjalan selama tab browser aktif.",
    }


def run_single_monitor_cycle(
    monitor_n=8,
    max_validations=1,
    interval_seconds=45,
    execution_mode="worker",
    worker_id="default_worker",
    tg_enabled=False,
    state=None,
    as_of=None,
):
    """Execute one complete headless or UI monitor + paper reconciliation cycle (PH07-01 / F29)."""
    target_state = ensure_analysis_state(state)
    extra_s = list(GROQ_KEYS) + list(GEMINI_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    ai_avail = bool(GROQ_KEYS or (GEMINI_ENABLED and GEMINI_KEYS))
    if _mon_mod is not None:
        return _mon_mod.run_monitor_cycle(
            monitor_n=monitor_n,
            max_validations=max_validations,
            interval_seconds=interval_seconds,
            execution_mode=execution_mode,
            worker_id=worker_id,
            db_file=DB_FILE,
            tg_enabled=tg_enabled,
            ai_available=ai_avail,
            state=target_state,
            momentum_fetcher=lambda n, return_report=True: momentum_candidates(n, as_of=as_of, return_report=return_report),
            workflow_processor=lambda snap, state=None, tg_enabled=False: process_monitor_candidate_workflow(
                snap, state=state, tg_enabled=tg_enabled
            ),
            pattern_saver=lambda snap: save_pattern_event(snap),
            reconcile_fn=lambda: update_paper_trades(return_detailed=True),
            as_of=as_of,
            extra_secrets=extra_s,
        )
    return {}


def create_background_monitor_worker(
    worker_id="default_worker",
    interval_seconds=45,
    monitor_n=8,
    max_validations=1,
    tg_enabled=False,
):
    """Create a controllable BackgroundMonitorWorker wired to the active runtime and SQLite DB (PH07-01 / F29)."""
    extra_s = list(GROQ_KEYS) + list(GEMINI_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    return BackgroundMonitorWorker(
        worker_id=worker_id,
        db_file=DB_FILE,
        interval_seconds=interval_seconds,
        monitor_n=monitor_n,
        max_validations=max_validations,
        tg_enabled=tg_enabled,
        ai_available_fn=lambda: bool(GROQ_KEYS or (GEMINI_ENABLED and GEMINI_KEYS)),
        momentum_fetcher=lambda n, return_report=True: momentum_candidates(n, return_report=return_report),
        workflow_processor=lambda snap, state=None, tg_enabled=False: process_monitor_candidate_workflow(
            snap, state=state, tg_enabled=tg_enabled
        ),
        pattern_saver=lambda snap: save_pattern_event(snap),
        reconcile_fn=lambda: update_paper_trades(return_detailed=True),
        extra_secrets=extra_s,
    )


def run_cli_entrypoint(argv=None, print_output=False, sleep_fn=time.sleep):
    """
    Headless CLI entrypoint for `--worker`, `--monitor-once`, `--reconcile-only`, and `--status`
    (PH07-01 / F29 / M7). Never launches Streamlit UI when CLI mode flags are passed.
    """
    global DB_FILE
    args = parse_cli_args(argv if argv is not None else sys.argv[1:])
    if not args.get("is_cli_mode"):
        return {"executed": False, "reason": "NOT_CLI_MODE", "args": args}

    if args.get("db_file"):
        DB_FILE = str(args["db_file"])
    init_db()

    tg_flag = bool(args.get("telegram") or (TG_TOKEN and TG_CHAT))
    w_id = str(args.get("worker_id") or "cli_worker")
    interval_s = int(args.get("interval") or 45)
    mon_n = int(args.get("monitor_n") or 8)
    max_ai = int(args.get("max_ai") or 1)

    if args.get("status"):
        st_out = get_runtime_monitor_status(
            auto_enabled=False,
            interval_seconds=interval_s,
            worker_id=w_id,
        )
        res = {"executed": True, "mode": "status", "status": st_out}
    elif args.get("reconcile_only"):
        upd = update_paper_trades(return_detailed=True)
        res = {"executed": True, "mode": "reconcile_only", "updates_count": len(upd), "updates": upd}
    elif args.get("monitor_once"):
        rep = run_single_monitor_cycle(
            monitor_n=mon_n,
            max_validations=max_ai,
            interval_seconds=interval_s,
            execution_mode="worker",
            worker_id=w_id,
            tg_enabled=tg_flag,
        )
        res = {"executed": True, "mode": "monitor_once", "report": rep}
    else:
        worker = create_background_monitor_worker(
            worker_id=w_id,
            interval_seconds=interval_s,
            monitor_n=mon_n,
            max_validations=max_ai,
            tg_enabled=tg_flag,
        )
        reports = worker.run_loop(max_cycles=args.get("max_cycles"), sleep_fn=sleep_fn)
        res = {
            "executed": True,
            "mode": "worker",
            "worker_id": w_id,
            "cycles_executed": len(reports),
            "reports": reports,
            "status": worker.status(),
        }

    if print_output:
        print(json.dumps(res, ensure_ascii=False, default=str))
    return res

# -------------------------
# UI
# -------------------------
def render_legacy_momentum_radar(configure_page=False):
    if configure_page:
        _configure_page()
    ensure_analysis_state(st.session_state)

    st.title("🚨 Momentum Radar")
    st.caption(
        "Pantau momentum crypto multi-timeframe, temukan setup yang berkembang, "
        "dan validasi peluang dengan AI. Semua transaksi bersifat paper trading."
    )

    # Sidebar: keep only the controls needed for the momentum workflow.
    st.sidebar.title("⚙️ Kontrol")
    st.sidebar.caption("AI: 🟢 Groq siap" if GROQ_KEYS else "AI: 🔴 Groq belum dikonfigurasi")
    st.sidebar.checkbox(
        "Mode AI proaktif",
        value=bool(st.session_state.get("v6_proactive_ai_mode", True)),
        key="v6_proactive_ai_mode",
        help="AI memilih LONG atau SHORT beserta level Entry, SL, dan TP saat setup memenuhi syarat.",
    )
    tg_on = st.sidebar.checkbox(
        "Kirim alert Telegram",
        value=bool(TG_TOKEN and TG_CHAT),
        key="v56_tg_alerts",
    )
    if not TG_TOKEN or not TG_CHAT:
        st.sidebar.caption("Telegram belum dikonfigurasi.")

    with st.sidebar.expander("Pengaturan & tools", expanded=False):
        if st.button("📨 Test Telegram", use_container_width=True, key="momentum_test_telegram"):
            ok, msg = telegram("✅ TEST MOMENTUM RADAR\nTelegram berhasil terhubung.")
            st.success(msg) if ok else st.error(msg)

        if st.button("🔄 Perbarui paper trades", use_container_width=True, key="momentum_update_paper"):
            with st.spinner("Memeriksa TP/SL paper trade..."):
                updates = update_paper_trades()
            if updates:
                for symbol, result, r in updates:
                    st.success(f"{symbol}: {result} ({r:+.1f}R)")
            else:
                st.info("Belum ada trade yang mencapai TP/SL.")

        st.divider()
        if st.button("🧹 Reset database", use_container_width=True, key="momentum_reset_database"):
            st.session_state.confirm_reset = True

        if st.session_state.get("confirm_reset", False):
            st.warning("Reset akan menghapus histori signal, agent votes, memory, pattern events, dan cache sesi aktif.")
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button("Ya, reset", key="momentum_confirm_reset"):
                reset_all_trading_state(state=st.session_state, confirmed=True)
                st.rerun()
            if cancel_col.button("Batal", key="momentum_cancel_reset"):
                st.session_state.confirm_reset = False
                st.rerun()

        st.divider()
        st.caption(f"Model analis: {ANALYST_MODEL}")
        st.caption(f"Model riset: {RESEARCH_MODEL}")
        st.caption(f"Jeda AI: {GROQ_MIN_DELAY:.1f}s · Timeout: {AI_HTTP_TIMEOUT_SECONDS:.0f}s")

    st.sidebar.caption("Paper trading saja. Sistem tidak mengirim order ke exchange.")

    # Main workflow: momentum is the primary and only discovery surface.
    monitor_col1, monitor_col2, monitor_col3 = st.columns([1, 1, 1])
    monitor_n = monitor_col1.slider("Pasangan dipantau", 4, 12, 8, key="v56_monitor_n")
    monitor_auto = monitor_col2.checkbox("🔄 Auto monitor", value=False, key="v56_auto")
    monitor_interval = monitor_col3.selectbox(
        "Interval pembaruan",
        [30, 45, 60, 90],
        index=1,
        format_func=lambda x: f"{x} detik",
        key="v56_interval",
    )
    with st.expander("Pengaturan validasi AI", expanded=False):
        max_validations = st.slider(
            "Maksimal validasi AI per siklus", 1, 2, 1, key="v56_max_ai"
        )

    rt_mon = get_runtime_monitor_status(
        auto_enabled=monitor_auto,
        interval_seconds=monitor_interval,
    )
    if rt_mon.get("worker_alive"):
        st.success(
            f"🟢 Monitor aktif · Siklus {rt_mon.get('cycles_completed', 0)} · "
            f"Heartbeat {rt_mon.get('heartbeat_age_seconds', 0.0):.1f} detik lalu"
        )
    else:
        st.caption(
            f"Status monitor: {rt_mon.get('runtime_label', 'SESSION_ONLY')} · "
            "Aktifkan Auto monitor atau jalankan pemindaian manual."
        )

    def _run_v56_monitor():
        if "v56_processed_fingerprints" not in st.session_state:
            st.session_state.v56_processed_fingerprints = set()

        with st.spinner("Menganalisis momentum, struktur, liquidity, dan pola harga..."):
            snaps = momentum_candidates(monitor_n)

        if not snaps:
            st.warning("Belum ada pasangan dengan data yang cukup. Coba lagi beberapa saat.")
            return

        rows = []
        for snap in snaps:
            pattern = snap["pattern"]
            rows.append({
                "Pair": snap["symbol"],
                "Setup": pattern["state"],
                "Arah": pattern["direction"],
                "Skor": pattern["score"],
                "5M": snap["smc"]["5m"]["timing"],
                "15M": snap["smc"]["15m"]["timing"],
                "1H": snap["smc"]["1h"]["structure"]["bias"],
                "24H": f"{snap['change']:+.2f}%",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        state_priority = {"TRIGGERED": 2, "DEVELOPING": 1}
        interesting = sorted(
            [x for x in snaps if x["pattern"]["state"] in {"DEVELOPING", "TRIGGERED"}],
            key=lambda x: (
                state_priority.get(str(x.get("pattern", {}).get("state", "")).upper(), 0),
                float(x.get("pattern", {}).get("score", 0) or 0),
            ),
            reverse=True,
        )
        if not interesting:
            st.info("Belum ada setup momentum yang cukup kuat. Sistem tidak memaksakan entry.")
            return

        st.markdown("### 🎯 Setup prioritas")
        validations = 0
        for snap in interesting:
            if validations >= max_validations:
                break

            pattern = snap["pattern"]
            icon = "🔥" if pattern["state"] == "TRIGGERED" else "👀"
            st.markdown(
                f"#### {icon} {snap['symbol']} · {pattern['state']} · "
                f"{pattern['direction']} · {pattern['score']}/100"
            )
            if pattern.get("evidence"):
                st.caption(" · ".join(pattern["evidence"][:4]))

            event_id, already_notified = save_pattern_event(snap)
            fingerprint = pattern_fingerprint(snap)
            if fingerprint in st.session_state.v56_processed_fingerprints:
                st.info("Setup ini sudah diproses. Menunggu perubahan struktur.")
                continue

            con = db()
            event_row = con.execute(
                "SELECT notified FROM pattern_events WHERE id=?", (event_id,)
            ).fetchone()
            con.close()
            if event_row and event_row["notified"] and pattern["state"] in {"TRIGGERED", "DEVELOPING"}:
                st.info("Setup ini sudah divalidasi atau dikirim sebagai alert. Menunggu pola baru.")
                continue

            if pattern["state"] in {"TRIGGERED", "DEVELOPING"} and GROQ_KEYS:
                try:
                    result = run_and_store_candidate_analysis(
                        snap["symbol"], pattern["score"], state=st.session_state
                    )
                    validations += 1
                    st.session_state.v56_processed_fingerprints.add(fingerprint)

                    modes = result.get("modes", {})
                    mode_cols = st.columns(3)
                    mode_labels = [
                        ("scalping", "⚡ Scalping"),
                        ("intraday", "📊 Intraday"),
                        ("swing", "🌊 Swing"),
                    ]
                    for col, (mode_key, label) in zip(mode_cols, mode_labels):
                        mode = modes.get(mode_key, {
                            "decision": "WAIT", "entry_action": "WAIT",
                            "directional_bias": "UNKNOWN", "confidence": 0, "reason": "-",
                        })
                        action = mode.get("entry_action", mode.get("decision", "WAIT"))
                        bias = mode.get("directional_bias", "UNKNOWN")
                        card_text = (
                            f"**{label} · {action}**\n\n"
                            f"Bias: {bias} · Skor: {mode.get('confidence', 0):.0f}/100\n\n"
                            f"{mode.get('reason', '-')}"
                        )
                        if action == "LONG":
                            col.success(card_text)
                        elif action == "SHORT":
                            col.error(card_text)
                        else:
                            col.warning(card_text)

                    gate_ok = bool(result.get("gate_report", {}).get("approved", False))
                    if result.get("final") in {"LONG", "SHORT"} and gate_ok:
                        st.success(
                            f"Final: {result['final']} · "
                            f"Confidence {result['portfolio']['confidence']:.0f}/100 · "
                            f"Timing {result.get('entry_status', '-')}"
                        )
                        try:
                            save_out = save_rendered_analysis(
                                result["analysis_id"],
                                state=st.session_state,
                                tg_enabled=tg_on and not already_notified,
                            )
                            st.caption(f"Paper trade tersimpan · ID {save_out['signal_id']}")
                            if save_out.get("tg_sent"):
                                mark_pattern_notified(event_id)
                                st.success("Alert Telegram terkirim.")
                            elif tg_on and not already_notified and save_out.get("created_new") and save_out.get("tg_msg"):
                                st.warning(f"Telegram belum terkirim: {save_out['tg_msg']}")
                        except Exception as save_err:
                            st.warning(f"Setup valid, tetapi paper trade gagal disimpan: {_safe_ui_error(save_err)}")
                    else:
                        confidence = result.get("portfolio", {}).get("confidence", 0)
                        st.warning(f"AI belum menyetujui entry. Confidence {confidence:.0f}/100.")
                except Exception as exc:
                    st.error(f"Validasi AI gagal untuk {snap['symbol']}: {_safe_ui_error(exc)}")
            elif pattern["state"] == "TRIGGERED":
                st.warning("Setup terpicu, tetapi Groq belum dikonfigurasi. AI belum memvalidasi entry.")
            else:
                st.info("Setup berkembang. Validasi AI menunggu trigger yang lebih kuat.")

    if monitor_auto:
        if hasattr(st, "fragment"):
            @st.fragment(run_every=f"{monitor_interval}s")
            def _auto_v56_fragment():
                _run_v56_monitor()
            _auto_v56_fragment()
        else:
            st.info("Auto monitor membutuhkan Streamlit yang mendukung fragment. Gunakan tombol scan manual.")

    if st.button("🚨 Scan Momentum Sekarang", use_container_width=True, key="v56_manual_scan"):
        _run_v56_monitor()

    # Secondary information stays available without competing with the momentum workflow.
    with st.expander("📊 Performa paper trading", expanded=False):
        perf = performance() or {}
        v6_perf = perf.get("v6_realistic") if isinstance(perf.get("v6_realistic"), dict) else None
        if v6_perf is not None:
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Equity", f"${v6_perf.get('current_equity', PAPER_INITIAL_EQUITY):,.2f}")
            p2.metric("Net PnL", f"${v6_perf.get('total_net_pnl', 0.0):+,.2f}")
            p3.metric("Trade terbuka", int(perf.get("open", 0) or 0))
            p4.metric("Win rate", f"{float(perf.get('win_rate', 0.0) or 0.0):.1f}%")
            analytics = v6_perf.get("analytics") or {}
            st.caption(
                f"Max drawdown: {analytics.get('max_drawdown_pct', 0.0):.2f}% · "
                f"Avg Net R: {v6_perf.get('avg_net_r', 0.0):+.2f}R · "
                f"Mode: {v6_perf.get('accounting_mode', REALISTIC_ACCOUNTING_MODE)}"
            )
        else:
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Trade tertutup", int(perf.get("closed", 0) or 0))
            p2.metric("Trade terbuka", int(perf.get("open", 0) or 0))
            p3.metric("Win rate", f"{float(perf.get('win_rate', 0.0) or 0.0):.1f}%")
            p4.metric("Average R", f"{float(perf.get('avg_r', 0.0) or 0.0):+.2f}R")
        if perf.get("accounting_disclaimer"):
            st.caption(perf["accounting_disclaimer"])

    with st.expander("🧠 Riwayat sinyal", expanded=False):
        con = db()
        history = con.execute(
            """
            SELECT id, created_at, symbol, final_decision, confidence,
                   entry, stop_loss, take_profit, result, r_multiple,
                   market_regime, smc_score, entry_status, raw_model_decision,
                   veto_reasons, accounting_mode
            FROM signals
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
        con.close()

        if history:
            history_rows = []
            for item in history:
                keys = item.keys()
                history_rows.append({
                    "ID": item["id"],
                    "Waktu": str(item["created_at"] or "-")[:19].replace("T", " "),
                    "Pair": item["symbol"],
                    "Decision": item["final_decision"],
                    "Confidence": round(float(item["confidence"] or 0)),
                    "Entry": item["entry"],
                    "SL": item["stop_loss"],
                    "TP": item["take_profit"],
                    "Status": format_signal_history_status(item),
                    "Raw model": item["raw_model_decision"] if "raw_model_decision" in keys and item["raw_model_decision"] else item["final_decision"],
                })
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)
        else:
            st.info("Belum ada sinyal tersimpan. Jalankan scan momentum untuk mulai memantau setup.")

    st.caption(
        "Momentum Radar untuk riset dan paper trading. Tidak mengeksekusi order otomatis. "
        "Hasil historis tidak menjamin hasil masa depan."
    )




# -------------------------------------------------------------
# Self-Contained Freqtrade Integration Bundle for GitHub / Streamlit Cloud
# Automatically provisions services/, pages/, strategies/, ft_client/, and user_data/
# even if only app.py was uploaded to the repository.
# -------------------------------------------------------------
_EMBEDDED_FREQTRADE_HASH = "2f110e57a1b2142e"
_EMBEDDED_FREQTRADE_B64 = "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"


def _ensure_freqtrade_integration_files(target_root=None):
    root_dir = os.path.abspath(target_root or _here)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    stamp_file = os.path.join(root_dir, ".ft_integration_bundle_hash")
    current_stamp = None
    if os.path.exists(stamp_file):
        try:
            with open(stamp_file, "r", encoding="utf-8") as _sf:
                current_stamp = _sf.read().strip()
        except Exception:
            current_stamp = None

    need_code_sync = (current_stamp != _EMBEDDED_FREQTRADE_HASH) or not os.path.exists(
        os.path.join(root_dir, "pages", "unified_dashboard.py")
    )
    if not need_code_sync and os.path.exists(os.path.join(root_dir, "user_data", "strategies", "SampleStrategy.py")):
        return

    try:
        import zlib as _zl, base64 as _b64
        payload = json.loads(_zl.decompress(_b64.b64decode(_EMBEDDED_FREQTRADE_B64.encode("ascii"))).decode("utf-8"))
        for rel_path, item in payload.items():
            dest_path = os.path.join(root_dir, *rel_path.split("/"))
            is_user_data = rel_path.startswith("user_data/")
            if is_user_data and os.path.exists(dest_path):
                continue
            if not is_user_data and not need_code_sync and os.path.exists(dest_path):
                continue
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if item.get("mode") == "b64":
                with open(dest_path, "wb") as _bf:
                    _bf.write(_b64.b64decode(item["data"].encode("ascii")))
            else:
                with open(dest_path, "w", encoding="utf-8") as _tf:
                    _tf.write(item["data"])
        with open(stamp_file, "w", encoding="utf-8") as _sf:
            _sf.write(_EMBEDDED_FREQTRADE_HASH)
    except Exception:
        pass


_ensure_freqtrade_integration_files(_here)


def render_app():
    """Render the unified Momentum Radar v5.5 + Freqtrade dashboard."""
    _configure_page()
    ensure_analysis_state(st.session_state)
    _ensure_freqtrade_integration_files(_here)
    try:
        from pages.unified_dashboard import render_unified_dashboard
        legacy_ctx = {
            "groq_keys": GROQ_KEYS,
            "gemini_keys": GEMINI_KEYS,
            "tg_token": TG_TOKEN,
            "tg_chat": TG_CHAT,
            "performance_fn": performance,
            "db_fn": db,
            "format_signal_status_fn": format_signal_history_status,
        }
        render_unified_dashboard(
            st,
            render_legacy_scanner_fn=lambda: render_legacy_momentum_radar(configure_page=False),
            legacy_context=legacy_ctx,
            project_root=_here,
        )
    except Exception as _ui_exc:
        # Fallback to legacy Momentum Radar if unified page module fails
        st.warning(f"Mode fallback UI aktif: {_safe_ui_error(_ui_exc)}")
        render_legacy_momentum_radar(configure_page=False)


if (__name__ == "__main__" or _is_streamlit_runtime()) and not os.getenv("AI_CONSENSUS_NO_AUTORUN"):
    if __name__ == "__main__" and not _is_streamlit_runtime() and parse_cli_args(sys.argv[1:]).get("is_cli_mode"):
        run_cli_entrypoint(sys.argv[1:], print_output=True)
    else:
        init_db()
        render_app()
