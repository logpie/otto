"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import { SavedCity, WeatherData, GeoLocation } from "@/lib/types";
import { fetchWeather, searchLocations, getBackgroundClass } from "@/lib/weather-api";
import { getWeatherIcon } from "@/lib/weather-icons";
import { MapPin, Search, X, Plus } from "lucide-react";

interface CityListProps {
  cities: SavedCity[];
  onSelectCity: (weather: WeatherData) => void;
  onAddCity: (city: SavedCity) => void;
  onRemoveCity: (cityId: string) => void;
}

interface CityWeatherCache {
  [cityId: string]: WeatherData;
}

export default function CityList({
  cities,
  onSelectCity,
  onAddCity,
  onRemoveCity,
}: CityListProps) {
  const [weatherCache, setWeatherCache] = useState<CityWeatherCache>({});
  const [searching, setSearching] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<GeoLocation[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const searchTimeout = useRef<NodeJS.Timeout>(undefined);
  const inputRef = useRef<HTMLInputElement>(null);

  // Fetch weather for all cities
  useEffect(() => {
    cities.forEach((city) => {
      if (!weatherCache[city.id]) {
        fetchWeather(city.latitude, city.longitude, city.name, city.region, city.country)
          .then((data) => {
            setWeatherCache((prev) => ({ ...prev, [city.id]: data }));
          })
          .catch(console.error);
      }
    });
  }, [cities]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSearch = useCallback((query: string) => {
    setSearchQuery(query);
    if (searchTimeout.current) clearTimeout(searchTimeout.current);

    if (query.length < 2) {
      setSearchResults([]);
      return;
    }

    setIsSearching(true);
    searchTimeout.current = setTimeout(async () => {
      try {
        const results = await searchLocations(query);
        setSearchResults(results);
      } catch (err) {
        console.error(err);
      } finally {
        setIsSearching(false);
      }
    }, 300);
  }, []);

  const handleAddCity = (location: GeoLocation) => {
    const newCity: SavedCity = {
      id: `${location.latitude}-${location.longitude}`,
      name: location.name,
      region: location.region,
      country: location.country,
      latitude: location.latitude,
      longitude: location.longitude,
    };
    onAddCity(newCity);
    setSearching(false);
    setSearchQuery("");
    setSearchResults([]);
  };

  const handleCityClick = (cityId: string) => {
    const weather = weatherCache[cityId];
    if (weather) {
      onSelectCity(weather);
    }
  };

  return (
    <div className="h-full bg-gradient-to-b from-gray-900 via-gray-900 to-black">
      <div className="main-scroll h-full">
        <div className="px-4 pt-12 pb-4">
          {/* Header */}
          <div className="flex items-center justify-between mb-4">
            <h1 className="text-3xl font-bold text-white">Weather</h1>
            {!searching && (
              <button
                onClick={() => {
                  setSearching(true);
                  setTimeout(() => inputRef.current?.focus(), 100);
                }}
                className="p-2 rounded-full hover:bg-white/10 transition-colors"
              >
                <Plus size={22} color="white" />
              </button>
            )}
          </div>

          {/* Search */}
          {searching ? (
            <div className="mb-4">
              <div className="flex items-center gap-2 bg-white/10 rounded-xl px-3 py-2.5">
                <Search size={18} className="text-white/50 shrink-0" />
                <input
                  ref={inputRef}
                  type="text"
                  value={searchQuery}
                  onChange={(e) => handleSearch(e.target.value)}
                  placeholder="Search for a city or airport"
                  className="search-input bg-transparent text-white outline-none w-full text-base"
                  autoFocus
                />
                <button
                  onClick={() => {
                    setSearching(false);
                    setSearchQuery("");
                    setSearchResults([]);
                  }}
                  className="shrink-0"
                >
                  <X size={18} className="text-white/50" />
                </button>
              </div>

              {/* Search Results */}
              {searchResults.length > 0 && (
                <div className="mt-2 rounded-xl overflow-hidden bg-gray-800/80 backdrop-blur-xl">
                  {searchResults.map((result, i) => (
                    <button
                      key={i}
                      onClick={() => handleAddCity(result)}
                      className="w-full px-4 py-3 flex items-center gap-3 hover:bg-white/10 transition-colors text-left border-b border-white/5 last:border-0"
                    >
                      <MapPin size={18} className="text-white/40 shrink-0" />
                      <div>
                        <div className="text-white font-medium">{result.name}</div>
                        <div className="text-white/50 text-sm">
                          {[result.region, result.country].filter(Boolean).join(", ")}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}

              {isSearching && (
                <div className="mt-4 text-center text-white/50 text-sm">Searching...</div>
              )}

              {searchQuery.length >= 2 && !isSearching && searchResults.length === 0 && (
                <div className="mt-4 text-center text-white/50 text-sm">No results found</div>
              )}
            </div>
          ) : (
            <div className="mb-4">
              <button
                onClick={() => {
                  setSearching(true);
                  setTimeout(() => inputRef.current?.focus(), 100);
                }}
                className="w-full flex items-center gap-2 bg-white/10 rounded-xl px-3 py-2.5"
              >
                <Search size={18} className="text-white/50" />
                <span className="text-white/50 text-base">Search for a city or airport</span>
              </button>
            </div>
          )}

          {/* City Cards */}
          <div className="space-y-3">
            {cities.map((city) => {
              const weather = weatherCache[city.id];
              const bgClass = weather
                ? getBackgroundClass(weather.current.condition.code, weather.current.isDay)
                : "bg-clear-day";

              return (
                <div
                  key={city.id}
                  className={`city-card ${bgClass} rounded-2xl p-4 cursor-pointer relative overflow-hidden min-h-[110px]`}
                  onClick={() => handleCityClick(city.id)}
                >
                  {/* Delete button */}
                  {!city.isCurrentLocation && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemoveCity(city.id);
                      }}
                      className="absolute top-2 right-2 p-1.5 rounded-full bg-black/20 hover:bg-black/40 transition-colors z-10 opacity-0 hover:opacity-100 group-hover:opacity-100"
                      style={{ opacity: undefined }}
                      onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                      onMouseLeave={(e) => (e.currentTarget.style.opacity = "0")}
                    >
                      <X size={14} color="white" />
                    </button>
                  )}

                  <div className="flex justify-between items-start relative z-[1]">
                    <div className="flex-1">
                      <div className="flex items-center gap-1.5">
                        {city.isCurrentLocation && <MapPin size={14} className="opacity-70" />}
                        <h3 className="text-xl font-semibold">{city.name}</h3>
                      </div>
                      <p className="text-xs opacity-70 mt-0.5">
                        {city.isCurrentLocation ? "My Location" : city.region || city.country}
                      </p>
                      {weather && (
                        <p className="text-sm opacity-80 mt-1">
                          {weather.current.condition.description}
                        </p>
                      )}
                    </div>
                    <div className="text-right flex flex-col items-end">
                      {weather ? (
                        <>
                          <div className="text-4xl font-light">{weather.current.temperature}°</div>
                          <div className="text-xs opacity-70 mt-1">
                            H:{weather.daily[0]?.tempHigh}° L:{weather.daily[0]?.tempLow}°
                          </div>
                        </>
                      ) : (
                        <div className="text-2xl font-light opacity-50">--°</div>
                      )}
                    </div>
                  </div>

                  {/* Background weather icon */}
                  {weather && (
                    <div className="absolute right-12 top-1/2 -translate-y-1/2 opacity-15 pointer-events-none">
                      {getWeatherIcon(weather.current.condition.icon, weather.current.isDay, 80)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
