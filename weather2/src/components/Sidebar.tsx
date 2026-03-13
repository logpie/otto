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
    <aside className="w-[320px] h-full bg-[#1c1c1e]/95 backdrop-blur-xl border-r border-white/[0.06] flex flex-col shrink-0">
      {/* Header - draggable titlebar region */}
      <div className="titlebar-drag px-4 pt-8 pb-2 flex items-center justify-between">
        <h1 className="text-[22px] font-bold text-white tracking-tight">Weather</h1>
        <button
          onClick={openSearch}
          className="titlebar-no-drag p-1.5 rounded-lg hover:bg-white/10 transition-colors"
          title="Add City"
        >
          <Plus size={18} className="text-white/70" />
        </button>
      </div>

      {/* Search Bar */}
      <div className="px-4 pb-2">
        {searching ? (
          <div>
            <div className="flex items-center gap-2 bg-white/[0.08] rounded-lg px-3 py-2">
              <Search size={15} className="text-white/40 shrink-0" />
              <input
                ref={inputRef}
                type="text"
                value={searchQuery}
                onChange={(e) => handleSearch(e.target.value)}
                placeholder="Search for a city"
                className="search-input bg-transparent text-white text-sm outline-none w-full"
                autoFocus
              />
              <button onClick={closeSearch} className="shrink-0">
                <X size={15} className="text-white/40 hover:text-white/70 transition-colors" />
              </button>
            </div>

            {/* Search Results Dropdown */}
            {searchResults.length > 0 && (
              <div className="mt-1.5 rounded-lg overflow-hidden bg-[#2c2c2e] border border-white/[0.06]">
                {searchResults.map((result, i) => (
                  <button
                    key={i}
                    onClick={() => handleAddCity(result)}
                    className="w-full px-3 py-2.5 flex items-center gap-2.5 hover:bg-white/[0.06] transition-colors text-left border-b border-white/[0.04] last:border-0"
                  >
                    <MapPin size={14} className="text-white/30 shrink-0" />
                    <div>
                      <div className="text-white text-sm font-medium">{result.name}</div>
                      <div className="text-white/40 text-xs">
                        {[result.region, result.country].filter(Boolean).join(", ")}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {isSearching && (
              <div className="mt-3 text-center text-white/30 text-xs">Searching...</div>
            )}

            {searchQuery.length >= 2 && !isSearching && searchResults.length === 0 && (
              <div className="mt-3 text-center text-white/30 text-xs">No results found</div>
            )}
          </div>
        ) : (
          <button
            onClick={openSearch}
            className="w-full flex items-center gap-2 bg-white/[0.06] rounded-lg px-3 py-2 hover:bg-white/[0.08] transition-colors"
          >
            <Search size={15} className="text-white/30" />
            <span className="text-white/30 text-sm">Search for a city</span>
          </button>
        )}
      </div>

      {/* City List */}
      <div className="flex-1 sidebar-scroll px-2 pb-4">
        <div className="space-y-0.5">
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
                {/* Mini gradient background */}
                <div
                  className={`absolute inset-0 ${bgClass} opacity-${isSelected ? "40" : "0"} transition-opacity duration-300 rounded-[10px]`}
                  style={{ opacity: isSelected ? 0.35 : 0 }}
                />

                <div className="relative px-3 py-3 flex items-center justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      {city.isCurrentLocation && (
                        <MapPin size={12} className="text-white/50 shrink-0" />
                      )}
                      <span className="text-[13px] font-semibold text-white truncate">
                        {city.isCurrentLocation ? "My Location" : city.name}
                      </span>
                    </div>
                    {city.isCurrentLocation && city.name !== "Current Location" ? (
                      <div className="text-[11px] text-white/40 mt-0.5 truncate">
                        {city.name}
                      </div>
                    ) : (
                      <div className="text-[11px] text-white/40 mt-0.5 truncate">
                        {weather?.current.condition.description || (city.region || city.country)}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    {weather && (
                      <div className="flex items-center gap-1.5">
                        <div className="w-5 h-5 flex items-center justify-center opacity-70">
                          {getWeatherIcon(weather.current.condition.icon, weather.current.isDay, 20)}
                        </div>
                        <span className="text-xl font-light text-white tabular-nums">
                          {weather.current.temperature}°
                        </span>
                      </div>
                    )}
                    {!weather && (
                      <span className="text-lg font-light text-white/30">--°</span>
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
                        <Trash2 size={12} className="text-white/40 hover:text-red-400" />
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
      <div className="px-4 py-3 border-t border-white/[0.06]">
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-white/20">
            Open-Meteo · {cities.length} {cities.length === 1 ? "city" : "cities"}
          </span>
        </div>
      </div>
    </aside>
  );
}
