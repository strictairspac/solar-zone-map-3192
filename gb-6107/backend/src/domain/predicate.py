from dataclasses import dataclass


@dataclass(frozen=True)
class Predicate:
    mode: str
    low: int | None = None
    high: int | None = None

    @classmethod
    def parse(cls, config):
        if isinstance(config, cls):
            return config
        if not isinstance(config, dict):
            raise ValueError("predicate 需要是对象")
        mode = config.get("mode")
        if mode == "null":
            return cls("null")
        if mode != "range":
            raise ValueError("predicate.mode 需要 range 或 null")
        low = config.get("low", config.get("low_cw"))
        high = config.get("high", config.get("high_cw"))
        # 兼容界面/HTTP 调用直接传 419.00–421.00 W 的两位小数。
        if isinstance(low, float) and low <= 500:
            low = round(low * 100)
        if isinstance(high, float) and high <= 500:
            high = round(high * 100)
        if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 500000:
            raise ValueError("需要 0 ≤ low ≤ high ≤ 500000 的整数厘瓦范围")
        return cls("range", low, high)

    def page_may_match(self, zone):
        if self.mode == "null":
            return zone.nulls > 0
        return zone.valid_mask != 0 and zone.minimum <= self.high and zone.maximum >= self.low

    def select_offsets(self, power, zone):
        if self.mode == "null":
            return [offset for offset in range(zone.count) if not zone.valid_mask & (1 << offset)]
        return [
            offset for offset, watts in enumerate(power)
            if zone.valid_mask & (1 << offset) and self.low <= watts <= self.high
        ]

    def matches_row(self, power):
        if self.mode == "null":
            return power is None
        return power is not None and self.low <= power <= self.high
