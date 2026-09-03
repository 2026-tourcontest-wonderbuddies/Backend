"""
routing/hybrid_engine.py의 HybridRoutingEngine을 서버 켜져있는 동안
한 번만 로드해서 재사용하기 위한 헬퍼. views.py에서 이 함수만 호출하면 됨.
"""
from apps.routing.hybrid_engine import HybridRoutingEngine

_routing_engine = None


def get_routing_engine():
    global _routing_engine
    if _routing_engine is None:
        _routing_engine = HybridRoutingEngine()
    return _routing_engine