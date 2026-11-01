"""Public, synthetic SMG-shaped fixtures exercise weather timestamp semantics."""

import unittest
from unittest.mock import patch

from scripts.weather_source import (
    CURRENT_URL,
    FORECAST_URL,
    WeatherValidationError,
    fetch_url,
    fetch_weather,
    parse_weather,
)


CURRENT = """<?xml version="1.0" encoding="UTF-8"?>
<ActualWeatherBrief>
 <System><SysPubdate>2026-11-01 08:55</SysPubdate></System>
 <Custom><ValidFor>2026-11-01 09:00</ValidFor>
  <Temperature><MeasureUnit>°C</MeasureUnit><Type>9</Type><Value>25</Value></Temperature>
  <Temperature><MeasureUnit>°C</MeasureUnit><Type>3</Type><Value>27</Value></Temperature>
  <Humidity><MeasureUnit>%</MeasureUnit><Type>3</Type><Value>77</Value></Humidity>
  <WindSpeed><MeasureUnit>km/h</MeasureUnit><Type>3</Type><Value>15</Value></WindSpeed>
  <Icon><IconDescription>間晴</IconDescription></Icon>
 </Custom>
</ActualWeatherBrief>"""

FORECAST = """<?xml version="1.0" encoding="UTF-8"?>
<ActualForecast>
 <System><SysPubdate>2026-11-01 09:00</SysPubdate></System>
 <Custom>
  <WeatherForecast><ValidFor>2026-11-01</ValidFor>
   <WeatherDescription>多雲，部份時間有陽光。
有幾陣微雨。氣溫約為攝氏25至30度。</WeatherDescription>
  </WeatherForecast>
  <WeatherForecast><ValidFor>2026-11-02</ValidFor>
   <WeatherDescription>多雲，氣溫約為攝氏24至29度。</WeatherDescription>
  </WeatherForecast>
 </Custom>
</ActualForecast>"""

FETCHED = "2026-11-01T01:54:03Z"


class WeatherSourceTests(unittest.TestCase):
    def parse(self, current=CURRENT, forecast=FORECAST, fetched=FETCHED):
        return parse_weather(current, forecast, fetched)

    def test_preserves_observed_published_and_fetched_times(self):
        result = self.parse()
        observed, today, tomorrow = result["rows"]
        self.assertEqual(observed["value"], 27)  # Type 9 minimum is not current.
        self.assertEqual(observed["humidity_percent"], 77)
        self.assertEqual(observed["wind_kmh"], 15)
        self.assertEqual(observed["quote_timestamp"], "2026-11-01T09:00:00+08:00")
        self.assertEqual(observed["published_at"], "2026-11-01T08:55:00+08:00")
        self.assertEqual(observed["fetch_timestamp"], FETCHED)
        self.assertFalse(observed["is_live"])
        self.assertEqual(today["id"], "macau_forecast")
        self.assertEqual(today["valid_date"], "2026-11-01")
        self.assertEqual(today["value"], {"low": 25, "high": 30})
        self.assertEqual(today["quote_timestamp"], "2026-11-01T09:00:00+08:00")
        self.assertEqual(tomorrow["id"], "macau_forecast_2026-11-02")
        self.assertEqual(result["source"]["executedAt"], FETCHED)

    def test_stale_observation_keeps_original_time(self):
        row = self.parse(fetched="2026-11-01T05:00:00Z")["rows"][0]
        self.assertEqual(row["status"], "stale")
        self.assertIn("2小時", row["warning"])
        self.assertEqual(row["quote_timestamp"], "2026-11-01T09:00:00+08:00")

    def test_expired_forecast_is_not_relabelled_today(self):
        result = self.parse(fetched="2026-11-03T01:00:00Z")
        self.assertNotIn("macau_forecast", [r["id"] for r in result["rows"]])
        self.assertTrue(all(r["status"] == "stale" for r in result["rows"][1:]))
        self.assertEqual(result["rows"][1]["valid_date"], "2026-11-01")
        self.assertIn("今日預報未有", result["rows"][0]["detail"])

    def test_missing_range_remains_missing(self):
        result = self.parse(forecast=FORECAST.replace("氣溫約為攝氏25至30度。", ""))
        self.assertEqual(result["rows"][1]["value"], {"low": None, "high": None})

    def test_rejects_non_finite_and_out_of_range_humidity(self):
        for value in ("NaN", "101", "-1", "not a number"):
            with self.subTest(value=value), self.assertRaises(WeatherValidationError):
                self.parse(current=CURRENT.replace("<Value>77</Value>", "<Value>" + value + "</Value>"))

    def test_rejects_duplicate_current_observation(self):
        duplicate = "<Temperature><MeasureUnit>°C</MeasureUnit><Type>3</Type><Value>28</Value></Temperature>"
        with self.assertRaises(WeatherValidationError):
            self.parse(current=CURRENT.replace("</Custom>", duplicate + "</Custom>"))

    def test_rejects_incorrect_unit(self):
        with self.assertRaises(WeatherValidationError):
            self.parse(current=CURRENT.replace("<MeasureUnit>°C</MeasureUnit>", "<MeasureUnit>°F</MeasureUnit>"))

    def test_rejects_duplicate_forecast_date(self):
        with self.assertRaises(WeatherValidationError):
            self.parse(forecast=FORECAST.replace("2026-11-02", "2026-11-01"))

    def test_rejects_reversed_forecast_range(self):
        with self.assertRaises(WeatherValidationError):
            self.parse(forecast=FORECAST.replace("攝氏25至30", "攝氏30至25"))

    def test_rejects_implausible_future_timestamp(self):
        with self.assertRaises(WeatherValidationError):
            self.parse(fetched="2026-11-01T00:00:00Z")

    def test_rejects_fetch_timestamp_without_timezone(self):
        with self.assertRaises(WeatherValidationError):
            self.parse(fetched="2026-11-01T01:54:03")

    def test_rejects_malformed_xml_and_dtd(self):
        for payload in ("<broken>", "<!DOCTYPE foo><ActualWeatherBrief/>"):
            with self.subTest(payload=payload), self.assertRaises(WeatherValidationError):
                self.parse(current=payload)

    def test_one_shot_fetch_uses_only_official_urls(self):
        with patch("scripts.weather_source.fetch_url", side_effect=[CURRENT, FORECAST]) as fetch:
            result = fetch_weather(FETCHED)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [CURRENT_URL, FORECAST_URL])
        self.assertEqual(result["rows"][0]["fetch_timestamp"], FETCHED)

    def test_network_failure_propagates_for_last_good_fallback(self):
        with patch("scripts.weather_source.fetch_url", side_effect=TimeoutError("timeout")):
            with self.assertRaises(TimeoutError):
                fetch_weather(FETCHED)

    def test_unapproved_url_fails_before_network_request(self):
        with patch("scripts.weather_source.urlopen") as network:
            with self.assertRaises(WeatherValidationError):
                fetch_url("https://example.com/weather")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
