import unittest

import requests

from apps.routing.hybrid_engine import (
    KAKAO_ROUTABLE_MAX_RATIO,
    HybridRoutingEngine,
    _haversine_m,
)


class _ExplodingSession:
    """호출되면 즉시 실패하는 가짜 session — 실제 HTTP 요청이 나가면 테스트가 실패한다."""

    def get(self, *args, **kwargs):
        raise AssertionError("kakao_routable=False인데 실제 HTTP 호출이 발생함")


class KakaoRoutablePrecheckTests(unittest.TestCase):
    """4.4 — kakao_routable 사전 체크가 카카오 호출을 실제로 생략하는지 검증."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = HybridRoutingEngine(api_key="dummy-key")
        ids = list(cls.engine._pos.keys())
        cls.origin_id, cls.destination_id = ids[0], ids[1]
        cls.i = cls.engine._pos[cls.origin_id]
        cls.j = cls.engine._pos[cls.destination_id]
        cls.original_distance_m = cls.engine.distances_m[cls.i, cls.j]

    def tearDown(self):
        # 다음 테스트가 깨끗한 매트릭스 값을 보도록 원복
        self.engine.distances_m[self.i, self.j] = self.original_distance_m
        self.engine.session = requests.Session()

    def test_normal_pair_is_routable(self):
        self.assertTrue(self.engine.kakao_routable(self.origin_id, self.destination_id))

    def test_ferry_like_detour_is_not_routable(self):
        straight_m = _haversine_m(
            self.engine._coords[self.origin_id], self.engine._coords[self.destination_id]
        )
        self.engine.distances_m[self.i, self.j] = straight_m * (KAKAO_ROUTABLE_MAX_RATIO + 1)
        self.assertFalse(self.engine.kakao_routable(self.origin_id, self.destination_id))

    def test_not_routable_pair_skips_http_call(self):
        straight_m = _haversine_m(
            self.engine._coords[self.origin_id], self.engine._coords[self.destination_id]
        )
        self.engine.distances_m[self.i, self.j] = straight_m * (KAKAO_ROUTABLE_MAX_RATIO + 1)
        self.engine.session = _ExplodingSession()

        result = self.engine._from_kakao(self.origin_id, self.destination_id, None)

        self.assertEqual(result["source"], "osrm_fallback")
        self.assertEqual(result["fallback_reason"], "not_routable")

    def test_routable_pair_still_attempts_http_call(self):
        self.engine.session = _ExplodingSession()

        with self.assertRaises(AssertionError):
            self.engine._from_kakao(self.origin_id, self.destination_id, None)
