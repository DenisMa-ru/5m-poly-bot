"""
Улучшенная версия функции analyze для crypto_bot.py
Включает:
1. Расширенный анализ микро-моментума (5 свечей вместо 2)
2. Анализ объема торгов
3. Учет более широкого рыночного контекста (15-минутный тренд)
"""

import requests
from typing import Dict, Any, List, Optional, Tuple

# Константы для анализа объема
VOLUME_NORMAL = 1.0  # Нормальный объем (множитель от среднего)
VOLUME_HIGH = 2.0    # Высокий объем (множитель от среднего)
VOLUME_PERIODS = 10  # Количество периодов для расчета среднего объема

def get_binance_candles(symbol: str, interval: str = "1m", limit: int = 6) -> list:
    """Fetches the last N candles from Binance."""
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=3
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[BINANCE ERROR] {e}")
        return []

def get_binance_price(symbol: str) -> float:
    """Current price from Binance."""
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=2
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return 0.0

def get_window_open_price(symbol: str, window_ts: int) -> float:
    """
    Fetches the open price of the current period from Binance.
    window_ts is the Unix timestamp of the 5-minute period start.
    """
    try:
        # Fetch the 5min candle corresponding to the period start
        r = requests.get(
            f"https://api.binance.com/api/v3/klines",
            params={
                "symbol":    symbol,
                "interval":  "5m",
                "startTime": window_ts * 1000,  # Binance uses milliseconds
                "limit":     1,
            },
            timeout=3
        )
        r.raise_for_status()
        candles = r.json()
        if candles:
            return float(candles[0][1])  # open price
        return 0.0
    except Exception:
        return 0.0

def get_atr(symbol: str, window_ts: int, periods: int = 5) -> float:
    """
    Calculates ATR (Average True Range) over the last N 5min periods.
    Returns the average range in USDC.
    """
    try:
        # Fetch periods candles ending at the current period start
        r = requests.get(
            f"https://api.binance.com/api/v3/klines",
            params={
                "symbol":   symbol,
                "interval": "5m",
                "endTime":  window_ts * 1000,  # up to the current period start
                "limit":    periods,
            },
            timeout=3
        )
        r.raise_for_status()
        candles = r.json()
        if not candles:
            return 0.0
        ranges = [float(c[2]) - float(c[3]) for c in candles]  # high - low
        return sum(ranges) / len(ranges)
    except Exception:
        return 0.0

def get_average_volume(symbol: str, interval: str = "5m", periods: int = VOLUME_PERIODS) -> float:
    """
    Calculates the average volume over the last N periods.
    Returns the average volume in base currency.
    """
    try:
        candles = get_binance_candles(symbol, interval, periods)
        if not candles:
            return 0.0
        volumes = [float(c[5]) for c in candles]  # volume in base currency
        return sum(volumes) / len(volumes)
    except Exception:
        return 0.0

def get_higher_timeframe_trend(symbol: str, interval: str = "15m", periods: int = 3) -> Optional[str]:
    """
    Determines the trend on a higher timeframe.
    Returns: "Up", "Down", or None if can't determine
    """
    try:
        candles = get_binance_candles(symbol, interval, periods)
        if len(candles) < periods:
            return None
            
        # Простой метод: сравниваем цену закрытия последней свечи с ценой открытия первой
        first_open = float(candles[0][1])
        last_close = float(candles[-1][4])
        
        if last_close > first_open:
            return "Up"
        elif last_close < first_open:
            return "Down"
        else:
            return None
    except Exception:
        return None

def analyze_micro_momentum(candles: List) -> Tuple[float, str]:
    """
    Анализирует микро-моментум на основе последних 5 1-минутных свечей.
    Возвращает: (вес_моментума, описание)
    """
    if len(candles) < 5:
        return 0, "недостаточно данных"
        
    # Получаем цены закрытия последних 5 свечей
    closes = [float(c[4]) for c in candles[-5:]]
    
    # Рассчитываем изменения между соседними свечами
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    
    # Определяем направление каждого изменения
    directions = [1 if change > 0 else (-1 if change < 0 else 0) for change in changes]
    
    # Взвешиваем изменения - более свежие имеют больший вес
    weights = [0.1, 0.2, 0.3, 0.4]  # Веса для 4 изменений (между 5 свечами)
    weighted_sum = sum(directions[i] * weights[i] for i in range(len(directions)))
    
    # Определяем общее направление моментума
    momentum_dir = "Up" if weighted_sum > 0 else "Down" if weighted_sum < 0 else "Flat"
    
    # Нормализуем вес моментума в диапазоне от 0 до 3
    momentum_weight = min(abs(weighted_sum) * 3, 3)
    
    # Формируем описание
    description = f"{momentum_dir} ({momentum_weight:.1f})"
    
    return momentum_weight if momentum_dir == "Up" else -momentum_weight, description

def analyze_volume(symbol: str, candles: List) -> Tuple[float, str]:
    """
    Анализирует объем торгов.
    Возвращает: (вес_объема, описание)
    """
    if not candles:
        return 0, "нет данных"
        
    # Получаем средний объем за последние N периодов
    avg_volume = get_average_volume(symbol)
    if avg_volume <= 0:
        return 0, "нет данных об объеме"
    
    # Получаем объем последней свечи
    current_volume = float(candles[-1][5])
    
    # Рассчитываем отношение текущего объема к среднему
    volume_ratio = current_volume / avg_volume
    
    # Определяем вес объема
    if volume_ratio >= VOLUME_HIGH:
        weight = 2.0
        desc = f"очень высокий ({volume_ratio:.1f}x)"
    elif volume_ratio >= VOLUME_NORMAL:
        weight = 1.0
        desc = f"повышенный ({volume_ratio:.1f}x)"
    else:
        weight = 0.0
        desc = f"нормальный ({volume_ratio:.1f}x)"
    
    return weight, desc

def improved_analyze(symbol: str, window_ts: int) -> dict:
    """
    Улучшенная версия функции analyze с:
    1. Расширенным анализом микро-моментума (5 свечей)
    2. Анализом объема торгов
    3. Учетом более широкого рыночного контекста (15-минутный тренд)
    
    Returns: расширенный словарь с результатами анализа
    """
    # Current price
    current_price = get_binance_price(symbol)
    if current_price <= 0:
        return {"confidence": 0, "direction": None, "reason": "no Binance price"}

    # Period open price
    window_open = get_window_open_price(symbol, window_ts)
    if window_open <= 0:
        # Fallback: use the open of the first 1min candle in the period
        candles_1m = get_binance_candles(symbol, "1m", 6)
        if candles_1m:
            window_open = float(candles_1m[0][1])
        else:
            return {"confidence": 0, "direction": None, "reason": "no open price"}

    # 1. Window Delta
    delta = (current_price - window_open) / window_open
    delta_pct = abs(delta) * 100
    delta_dir = "Up" if delta > 0 else "Down"

    # ATR — volatility filter
    atr = get_atr(symbol, window_ts, 5)
    if atr > 0:
        candles_5m = get_binance_candles(symbol, "5m", 1)
        if candles_5m:
            current_range = float(candles_5m[0][2]) - float(candles_5m[0][3])  # high - low
            if current_range > atr * 2.0:  # Увеличенный множитель ATR
                return {
                    "confidence":    0,
                    "direction":     None,
                    "window_open":   window_open,
                    "current_price": current_price,
                    "delta_pct":     delta_pct,
                    "atr":           atr,
                    "current_range": current_range,
                    "reason":        f"ATR skip: range ${current_range:.2f} > 2.0x ATR ${atr:.2f}",
                }

    # Минимальная дельта увеличена до 0.0015 (0.15%)
    if abs(delta) < 0.0015:
        return {
            "confidence":    0,
            "direction":     None,
            "window_open":   window_open,
            "current_price": current_price,
            "delta_pct":     delta_pct,
            "reason":        f"delta {delta_pct:.4f}% < 0.15% — too close to the line",
        }

    # Delta weight - увеличены пороги
    if abs(delta) >= 0.01:      # > 1.0%
        delta_weight = 7
    elif abs(delta) >= 0.003:   # > 0.3%
        delta_weight = 5
    elif abs(delta) >= 0.0015:  # > 0.15%
        delta_weight = 3
    else:
        delta_weight = 1

    # Начальный счет на основе дельты
    score = delta_weight if delta > 0 else -delta_weight

    # 2. Расширенный микро-моментум (5 свечей вместо 2)
    candles_1m = get_binance_candles(symbol, "1m", 6)
    if len(candles_1m) >= 5:
        momentum_weight, momentum_desc = analyze_micro_momentum(candles_1m)
        
        # Добавляем вес моментума только если он совпадает с направлением дельты
        if (delta > 0 and momentum_weight > 0) or (delta < 0 and momentum_weight < 0):
            score += abs(momentum_weight)
            momentum_str = f"{momentum_desc} (подтверждает)"
        else:
            momentum_str = f"{momentum_desc} (противоречит, игнорируется)"
    else:
        momentum_str = "нет данных"

    # 3. Анализ объема
    volume_weight, volume_desc = analyze_volume(symbol, candles_1m)
    if volume_weight > 0:
        # Добавляем вес объема к счету
        score += volume_weight if delta > 0 else -volume_weight
        volume_str = f"{volume_desc} (усиливает сигнал)"
    else:
        volume_str = f"{volume_desc}"

    # 4. Анализ более широкого рыночного контекста (15-минутный тренд)
    higher_trend = get_higher_timeframe_trend(symbol, "15m", 3)
    if higher_trend:
        # Если тренд на старшем таймфрейме совпадает с дельтой, добавляем вес
        if (delta > 0 and higher_trend == "Up") or (delta < 0 and higher_trend == "Down"):
            score += 2
            trend_str = f"{higher_trend} (подтверждает, +2)"
        else:
            trend_str = f"{higher_trend} (противоречит, игнорируется)"
    else:
        trend_str = "неопределенный"

    # Максимально возможный счет теперь 14 (7 + 3 + 2 + 2)
    # Confidence (нормализованная)
    confidence = min(abs(score) / 14.0, 1.0)
    direction = "Up" if score > 0 else "Down"

    return {
        "score":         score,
        "confidence":    confidence,
        "direction":     direction,
        "window_open":   window_open,
        "current_price": current_price,
        "delta_pct":     delta_pct,
        "delta_weight":  delta_weight,
        "momentum":      momentum_str,
        "volume":        volume_str,
        "higher_trend":  trend_str,
        "atr":           atr if 'atr' in locals() else 0,
        "reason":        f"delta={delta_pct:.4f}% ({delta_dir}, w={delta_weight}) momentum={momentum_str} volume={volume_str} trend={trend_str}",
    }

# Пример использования:
# Замените функцию analyze в crypto_bot.py на improved_analyze
# и обновите импорты в начале файла