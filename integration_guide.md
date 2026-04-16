# Руководство по интеграции улучшений в 5m-poly-bot

В этом руководстве описаны шаги по интеграции предложенных улучшений в ваш торговый бот, а также дополнительные рекомендации для повышения его эффективности.

## 1. Оптимизированные настройки

Файл `settings.json` уже создан с оптимизированными параметрами:

```json
{
  "bank": 100,
  "mode": "dry-run",
  "amount": 10,
  "min_confidence": 0.55,
  "entry_min": 10,
  "entry_max": 30,
  "price_min_btc": 0.95,
  "price_min_eth": 0.93,
  "price_max": 0.99,
  "delta_skip": 0.0015,
  "atr_multiplier": 2.0
}
```

Эти настройки соответствуют более консервативной стратегии, которая должна снизить количество убыточных сделок за счет более строгих критериев входа.

## 2. Интеграция улучшенной функции анализа

Файл `improved_analyze.py` содержит улучшенную версию функции анализа с расширенным микро-моментумом, анализом объема торгов и учетом более широкого рыночного контекста.

### Шаги по интеграции:

1. **Добавьте новые импорты** в начало файла `crypto_bot.py`:

```python
from typing import Dict, Any, List, Optional, Tuple
```

2. **Замените функцию `analyze`** в файле `crypto_bot.py` на функцию `improved_analyze` из файла `improved_analyze.py`.

3. **Добавьте новые вспомогательные функции** из файла `improved_analyze.py` в файл `crypto_bot.py`:
   - `get_average_volume`
   - `get_higher_timeframe_trend`
   - `analyze_micro_momentum`
   - `analyze_volume`

4. **Добавьте новые константы** в секцию CONFIG файла `crypto_bot.py`:

```python
# Константы для анализа объема
VOLUME_NORMAL = 1.0  # Нормальный объем (множитель от среднего)
VOLUME_HIGH = 2.0    # Высокий объем (множитель от среднего)
VOLUME_PERIODS = 10  # Количество периодов для расчета среднего объема
```

## 3. Дополнительные улучшения управления рисками

Для дальнейшего улучшения работы бота рекомендуется внести следующие изменения в класс `CryptoBot`:

### 3.1. Динамическое определение размера ставки

Добавьте в метод `_evaluate_entry` класса `CryptoBot` следующий код для динамического определения размера ставки в зависимости от уверенности сигнала:

```python
def _evaluate_entry(self, market, ta, seconds_left, entered_slugs):
    # ... существующий код ...
    
    # Динамический размер ставки в зависимости от уверенности
    confidence = ta.get("confidence", 0)
    if confidence >= 0.8:  # Очень высокая уверенность
        trade_amount = self.amount * 1.5
    elif confidence >= 0.6:  # Высокая уверенность
        trade_amount = self.amount * 1.2
    else:  # Средняя уверенность
        trade_amount = self.amount
    
    # ... далее используйте trade_amount вместо self.amount ...
    
    # Обновите signal_data
    signal_data["amount"] = trade_amount
    
    # ... остальной код ...
    
    # Передайте trade_amount в _enter
    self._enter(market, ta, seconds_left, trade_amount)
```

И соответственно обновите метод `_enter`:

```python
def _enter(self, market: dict, ta: dict, seconds_left: float, trade_amount: float = None):
    if trade_amount is None:
        trade_amount = self.amount
        
    # ... используйте trade_amount вместо self.amount ...
```

### 3.2. Ограничение максимальных потерь

Добавьте в класс `CryptoBot` следующие атрибуты и методы для ограничения максимальных потерь:

```python
def __init__(self, paper: bool, dry_run: bool, amount: float):
    # ... существующий код ...
    
    # Лимиты потерь
    self.daily_loss_limit = float(settings.get("daily_loss_limit", 20.0))  # Максимальный убыток за день
    self.daily_losses = 0.0  # Текущие убытки за день
    self.last_day = datetime.now().day  # Для сброса счетчика убытков
    
def _check_loss_limits(self) -> bool:
    """Проверяет, не превышены ли лимиты потерь. Возвращает True, если торговля разрешена."""
    # Сброс счетчика убытков при смене дня
    current_day = datetime.now().day
    if current_day != self.last_day:
        self.daily_losses = 0.0
        self.last_day = current_day
        
    # Проверка лимита дневных убытков
    if self.daily_losses >= self.daily_loss_limit:
        log(f"⚠️ Достигнут дневной лимит убытков (${self.daily_losses:.2f} >= ${self.daily_loss_limit:.2f}). Торговля приостановлена до завтра.")
        return False
        
    return True
```

И добавьте вызов этой проверки в метод `_evaluate_entry`:

```python
def _evaluate_entry(self, market, ta, seconds_left, entered_slugs):
    # ... существующий код ...
    
    # Проверка лимитов потерь
    if not self._check_loss_limits():
        signal_data["reason"] = "достигнут дневной лимит убытков"
        save_signal(signal_data)
        return
    
    # ... остальной код ...
```

Также обновите метод `_check_previous_round` для учета убытков:

```python
def _check_previous_round(self, close_ts: int):
    # ... существующий код ...
    
    # В блоке, где определяется результат сделки:
    if not won:
        # Проигрыш: теряем ставку
        realized_pnl = -trade_amount
        result = "LOSS"
        # Учитываем убыток в дневном лимите
        self.daily_losses += trade_amount
    
    # ... остальной код ...
```

### 3.3. Фильтрация по времени

Добавьте в класс `CryptoBot` метод для фильтрации по времени:

```python
def _is_trading_allowed_time(self) -> bool:
    """Проверяет, разрешена ли торговля в текущее время."""
    # Получаем текущее время в UTC
    now = datetime.now(timezone.utc)
    hour = now.hour
    
    # Определяем периоды высокой волатильности (например, открытие/закрытие основных бирж)
    # Например, избегаем торговли в периоды:
    # - Открытие азиатских бирж: 00:00-01:00 UTC
    # - Открытие европейских бирж: 07:00-08:00 UTC
    # - Открытие американских бирж: 13:30-14:30 UTC
    # - Закрытие американских бирж: 20:00-21:00 UTC
    high_volatility_hours = [0, 1, 7, 8, 13, 14, 20, 21]
    
    if hour in high_volatility_hours:
        return False
    
    return True
```

И добавьте вызов этой проверки в метод `_evaluate_entry`:

```python
def _evaluate_entry(self, market, ta, seconds_left, entered_slugs):
    # ... существующий код ...
    
    # Фильтрация по времени
    if not self._is_trading_allowed_time():
        log(f"   [{crypto}] SKIP — высокая волатильность в текущий час")
        signal_data["reason"] = "высокая волатильность в текущий час"
        save_signal(signal_data)
        return
    
    # ... остальной код ...
```

## 4. Добавление настроек в settings.json

Добавьте следующие настройки в файл `settings.json`:

```json
{
  "bank": 100,
  "mode": "dry-run",
  "amount": 10,
  "min_confidence": 0.55,
  "entry_min": 10,
  "entry_max": 30,
  "price_min_btc": 0.95,
  "price_min_eth": 0.93,
  "price_max": 0.99,
  "delta_skip": 0.0015,
  "atr_multiplier": 2.0,
  "daily_loss_limit": 20.0,
  "dynamic_sizing": true,
  "time_filtering": true
}
```

## 5. План тестирования

После внедрения изменений рекомендуется следующий план тестирования:

1. **Запустите бота в режиме dry-run** на несколько дней для сбора данных о работе с новыми настройками.

2. **Сравните результаты** с предыдущей версией бота:
   - Win/Loss ratio
   - Общий PnL
   - Количество сделок
   - Средний размер прибыли/убытка

3. **Проанализируйте логи** для выявления возможных проблем или дальнейших улучшений.

4. **Постепенно переходите к режиму live** с небольшими суммами, если результаты тестирования положительные.

## 6. Мониторинг и дальнейшая оптимизация

1. **Регулярно анализируйте результаты** торговли и корректируйте настройки при необходимости.

2. **Рассмотрите возможность добавления** дополнительных индикаторов и фильтров:
   - RSI (Relative Strength Index)
   - MACD (Moving Average Convergence Divergence)
   - Bollinger Bands

3. **Автоматизируйте оптимизацию параметров** с использованием исторических данных.

## Заключение

Предложенные улучшения должны значительно повысить эффективность бота за счет:
- Более строгих критериев входа
- Расширенного анализа рынка
- Улучшенного управления рисками
- Технических улучшений

Рекомендуется внедрять изменения постепенно, тщательно тестируя каждое изменение перед переходом к следующему.