"""Погода Open-Meteo, фиксированные HTTPS endpoints, ограниченные read-повторы."""

import asyncio
import math

import aiohttp

from app.mcp_service.contracts import Place, WeatherOutput


class ToolFailure(Exception):
    """Код ошибки без тела внешнего ответа."""


class Weather:
    def __init__(self, session: aiohttp.ClientSession, timeout: int = 10):
        self.session, self.timeout = session, timeout

    async def get_json(self, url, params):
        for attempt in range(2):
            try:
                async with self.session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ToolFailure("weather_unavailable")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 1_000_000:
                            raise ToolFailure("weather_invalid_response")
                    import json

                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
            except (TimeoutError, aiohttp.ClientError, OSError, ToolFailure) as exc:
                if attempt:
                    raise ToolFailure(
                        "weather_timeout"
                        if isinstance(exc, TimeoutError)
                        else "weather_unavailable"
                    ) from None
                await asyncio.sleep(0.2)
            except (ValueError, UnicodeError):
                raise ToolFailure("weather_invalid_response") from None
        raise ToolFailure("weather_unavailable")

    async def current(self, city: str) -> WeatherOutput:
        data = await self.get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": city, "count": 100, "language": "ru", "format": "json"},
        )
        rows = data.get("results", [])
        if data.get("error"):
            raise ToolFailure("weather_invalid_response")
        if not isinstance(rows, list):
            raise ToolFailure("weather_invalid_response")
        if not rows:
            return WeatherOutput(status="not_found", source="Open-Meteo")
        try:
            unique = {}
            for row in rows:
                # Open-Meteo omits unavailable fields (e.g. country for MS).
                # Keep the candidate and its ISO code instead of losing the
                # whole search or silently choosing a different location.
                country = row.get("country") or row.get("country_code")
                if not isinstance(country, str) or not country.strip():
                    raise ValueError
                place = Place(
                    name=row["name"],
                    country=country,
                    region=row.get("admin1", ""),
                    query=", ".join([row["name"], row.get("admin1") or country]),
                )
                unique[(row["latitude"], row["longitude"])] = (row, place)
            candidates = list(unique.values())
            if len(candidates) != 1:
                return WeatherOutput(
                    status="ambiguous",
                    source="Open-Meteo",
                    options=[place for _, place in candidates],
                )
            row, place = candidates[0]
            data = await self.get_json(
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                    "wind_speed_unit": "ms",
                },
            )
            current, units = data["current"], data["current_units"]
            temperature, wind = float(current["temperature_2m"]), float(current["wind_speed_10m"])
            if (
                not math.isfinite(temperature)
                or not math.isfinite(wind)
                or wind < 0
                or units["temperature_2m"] != "°C"
            ):
                raise ValueError
            return WeatherOutput(
                status="ok",
                source="Open-Meteo",
                place=place,
                temperature_c=temperature,
                weather_code=current["weather_code"],
                wind_speed=wind,
                wind_unit=units["wind_speed_10m"],
                observed_at=current["time"],
                timezone=data["timezone"],
            )
        except (KeyError, TypeError, ValueError):
            raise ToolFailure("weather_invalid_response") from None
