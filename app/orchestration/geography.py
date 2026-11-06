"""Фиксированные Open-Meteo endpoints; координаты только из подтверждённого города."""

import math

from app.mcp_service.contracts import Place, WeatherOutput
from app.mcp_service.weather import ToolFailure
from app.orchestration.contracts import City

DEFAULT_CITY = City(
    name="Санкт-Петербург",
    country="Россия",
    region="Санкт-Петербург",
    latitude=59.9386,
    longitude=30.3141,
)


class Geography:
    def __init__(self, weather):
        self.weather = weather

    async def search(self, query):
        name, _, hint = query.partition(",")
        data = await self.weather.get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": name.strip(), "count": 100, "language": "ru", "format": "json"},
        )
        rows = data.get("results", [])
        if not isinstance(rows, list):
            raise ToolFailure("weather_invalid_response")
        cities = {}
        for row in rows:
            country = row.get("country") or row.get("country_code")
            region = row.get("admin1", "")
            if hint.strip() and hint.strip().casefold() not in {
                str(country).casefold(),
                str(row.get("country_code", "")).casefold(),
                region.casefold(),
                (str(country) + ", " + region).casefold(),
                (region + ", " + str(country)).casefold(),
            }:
                continue
            city = City(
                name=row["name"],
                country=country,
                region=region,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            )
            cities[(city.latitude, city.longitude)] = city
        return list(cities.values())[:10]

    async def current(self, city):
        city = City.model_validate(city)
        data = await self.weather.get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": city.latitude,
                "longitude": city.longitude,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "timezone": "auto",
                "wind_speed_unit": "ms",
            },
        )
        try:
            item, units = data["current"], data["current_units"]
            temp, wind = float(item["temperature_2m"]), float(item["wind_speed_10m"])
            if (
                not math.isfinite(temp)
                or not math.isfinite(wind)
                or wind < 0
                or units["temperature_2m"] != "°C"
            ):
                raise ValueError
            return WeatherOutput(
                status="ok",
                source="Open-Meteo",
                temperature_c=temp,
                weather_code=item["weather_code"],
                wind_speed=wind,
                wind_unit=units["wind_speed_10m"],
                observed_at=item["time"],
                timezone=data["timezone"],
                place=Place(
                    name=city.name,
                    country=city.country,
                    region=city.region,
                    query=city.name + ", " + city.country,
                ),
            )
        except (ValueError, KeyError, TypeError):
            raise ToolFailure("weather_invalid_response") from None
