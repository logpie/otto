import React, { useState, useEffect, useCallback, useRef } from "react";
import { SavedCity, WeatherData, GeoLocation, FetchTimings } from "@/lib/types";
import { fetchWeather, searchLocations, getBackgroundClass } from "@/lib/weather-api";
import { getWeatherIcon } from "@/lib/weather-icons";
import { MapPin, Search, X, Plus, Trash2 } from "lucide-react";

interface SidebarProps {
  cities: SavedCity[];
  selectedCityId: string | null;
  weatherCache: Record<string, WeatherData>;
  onSelectCity: (cityId: string) => void;
  onAddCity: (city: SavedCity) => void;
  onRemoveCity: (cityId: string) => void;
  onUpdateWeather: (cityId: string, data: WeatherData) => void;
  onUpdateTimings?: (cityId: string, timings: FetchTimings) => void;
}

export default function Sidebar({
  cities,
  selectedCityId,
  weatherCache,
  onSelectCity,
  onAddCity,
  onRemoveCity,
  onUpdateWeather,
  onUpdateTimings,
}: SidebarProps) {
  const [searching, setSearching] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<GeoLocation[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [hoveredCity, setHoveredCity] = useState<string | null>(null);
  const searchTimeout = useRef<NodeJS.Timeout>(undefined);
  const inputRef = useRef<HTMLInputElement>(null);

  // Fetch weather for all cities
  useEffect(() => {
    cities.forEach((city) => {
      if (!weatherCache[city.id]) {
        fetchWeather(city.latitude, city.longitude, city.name, city.region, city.country)
          .then(({ weather, timings }) => {
            onUpdateWeather(city.id, weather);
            onUpdateTimings?.(city.id, timings);
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
    }, 150);
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

  const openSearch = () => {
    setSearching(true);
    setTimeout(() => inputRef.current?.focus(), 50);
  };

  const closeSearch = () => {
    setSearching(false);
    setSearchQuery("");
    setSearchResults([]);
  };

  return (
    <aside className="w-[300px] h-full bg-[#1c1c1e]/[0.97] backdrop-blur-2xl border-r border-white/[0.04] flex flex-col shrink-0">
      {/* Header - draggable titlebar region */}
      <div className="titlebar-drag px-5 pt-9 pb-3 flex items-center justify-between">
        <h1 className="text-[20px] font-semibold text-white/90 tracking-tight">Weather</h1>
        <button
          onClick={openSearch}
          className="titlebar-no-drag p-1.5 rounded-lg hover:bg-white/10 transition-colors"
          title="Add City"
        >
          <Plus size={17} className="text-white/50" />
        </button>
      </div>

      {/* Search Bar */}
      <div className="px-4 pb-2.5">
        {searching ? (
          <div>
            <div className="flex items-center gap-2 bg-white/[0.06] rounded-[10px] px-3 py-[7px]">
              <Search size={14} className="text-white/35 shrink-0" />
              <input
                ref={inputRef}
                type="text"
                value={searchQuery}
                onChange={(e) => handleSearch(e.target.value)}
                placeholder="Search for a city"
                className="search-input bg-transparent text-white text-[13px] outline-none w-full"
                autoFocus
              />
              <button onClick={closeSearch} className="shrink-0">
                <X size={14} className="text-white/35 hover:text-white/60 transition-colors" />
              </button>
            </div>

            {/* Search Results Dropdown */}
            {searchResults.length > 0 && (
              <div className="mt-1.5 rounded-[10px] overflow-hidden bg-[#2c2c2e]/95 backdrop-blur-xl border border-white/[0.06]">
                {searchResults.map((result, i) => (
                  <button
                    key={i}
                    onClick={() => handleAddCity(result)}
                    className="w-full px-3 py-2.5 flex items-center gap-2.5 hover:bg-white/[0.06] transition-colors text-left border-b border-white/[0.04] last:border-0"
                  >
                    <MapPin size={13} className="text-white/25 shrink-0" />
                    <div>
                      <div className="text-white text-[13px] font-medium">{result.name}</div>
                      <div className="text-white/35 text-[11px]">
                        {[result.region, result.country].filter(Boolean).join(", ")}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {isSearching && (
              <div className="mt-3 text-center text-white/25 text-[11px]">Searching...</div>
            )}

            {searchQuery.length >= 2 && !isSearching && searchResults.length === 0 && (
              <div className="mt-3 text-center text-white/25 text-[11px]">No results found</div>
            )}
          </div>
        ) : (
          <button
            onClick={openSearch}
            className="w-full flex items-center gap-2 bg-white/[0.04] rounded-[10px] px-3 py-[7px] hover:bg-white/[0.06] transition-colors"
          >
            <Search size={14} className="text-white/25" />
            <span className="text-white/25 text-[13px]">Search for a city</span>
          </button>
        )}
      </div>

      {/* City List */}
      <div className="flex-1 sidebar-scroll px-2.5 pb-4">
        <div className="space-y-[2px]">
          {cities.map((city) => {
            const weather = weatherCache[city.id];
            const isSelected = city.id === selectedCityId;
            const isHovered = city.id === hoveredCity;
            const bgClass = weather
              ? getBackgroundClass(weather.current.condition.code, weather.current.isDay)
              : "bg-clear-day";

            return (
              <div
                key={city.id}
                className={`sidebar-city relative overflow-hidden ${isSelected ? "active" : ""}`}
                onClick={() => onSelectCity(city.id)}
                onMouseEnter={() => setHoveredCity(city.id)}
                onMouseLeave={() => setHoveredCity(null)}
              >
                {/* Mini gradient background for selected */}
                <div
                  className={`absolute inset-0 ${bgClass} rounded-[12px] transition-opacity duration-300`}
                  style={{ opacity: isSelected ? 0.4 : 0 }}
                />

                <div className="relative px-3.5 py-3.5 flex items-center justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      {city.isCurrentLocation && (
                        <MapPin size={11} className="text-white/45 shrink-0" />
                      )}
                      <span className="text-[15px] font-semibold text-white truncate leading-tight">
                        {city.isCurrentLocation ? "My Location" : city.name}
                      </span>
                    </div>
                    {city.isCurrentLocation && city.name !== "Current Location" ? (
                      <div className="text-[11px] text-white/35 mt-1 truncate">
                        {city.name}
                      </div>
                    ) : (
                      <div className="text-[11px] text-white/35 mt-1 truncate">
                        {weather?.current.condition.description || (city.region || city.country)}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 shrink-0 ml-3">
                    {weather && (
                      <span className="text-[24px] font-light text-white tabular-nums tracking-tight">
                        {weather.current.temperature}°
                      </span>
                    )}
                    {!weather && (
                      <span className="text-[20px] font-light text-white/25 tabular-nums">--°</span>
                    )}

                    {/* Delete button on hover */}
                    {!city.isCurrentLocation && isHovered && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onRemoveCity(city.id);
                        }}
                        className="p-1 rounded-md hover:bg-white/10 transition-colors"
                      >
                        <Trash2 size={11} className="text-white/35 hover:text-red-400" />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Footer */}
      <div className="px-5 py-3 border-t border-white/[0.04]">
        <span className="text-[10px] text-white/15">
          Open-Meteo · {cities.length} {cities.length === 1 ? "city" : "cities"}
        </span>
      </div>
    </aside>
  );
}
