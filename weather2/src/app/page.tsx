"use client";

import React, { useState, useEffect, useCallback } from "react";
import { SavedCity, WeatherData } from "@/lib/types";
import Sidebar from "@/components/Sidebar";
import WeatherDetail from "@/components/WeatherDetail";

const DEFAULT_CITIES: SavedCity[] = [
  {
    id: "40.7128--74.006",
    name: "New York",
    region: "New York",
    country: "United States",
    latitude: 40.7128,
    longitude: -74.006,
  },
  {
    id: "37.7749--122.4194",
    name: "San Francisco",
    region: "California",
    country: "United States",
    latitude: 37.7749,
    longitude: -122.4194,
  },
  {
    id: "51.5074--0.1278",
    name: "London",
    region: "England",
    country: "United Kingdom",
    latitude: 51.5074,
    longitude: -0.1278,
  },
];

const STORAGE_KEY = "weather-app-cities";

function loadCities(): SavedCity[] {
  if (typeof window === "undefined") return DEFAULT_CITIES;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch {}
  return DEFAULT_CITIES;
}

function saveCities(cities: SavedCity[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cities));
  } catch {}
}

export default function Home() {
  const [cities, setCities] = useState<SavedCity[]>(DEFAULT_CITIES);
  const [selectedCityId, setSelectedCityId] = useState<string | null>(null);
  const [weatherCache, setWeatherCache] = useState<Record<string, WeatherData>>({});
  const [initialLoading, setInitialLoading] = useState(true);

  // Load cities from localStorage
  useEffect(() => {
    const loaded = loadCities();
    setCities(loaded);
    if (loaded.length > 0) {
      setSelectedCityId(loaded[0].id);
    }
    setInitialLoading(false);
  }, []);

  // Try to get user's location
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        async (position) => {
          try {
            const { latitude, longitude } = position.coords;
            const locationCity: SavedCity = {
              id: "current-location",
              name: "Current Location",
              region: "",
              country: "",
              latitude,
              longitude,
              isCurrentLocation: true,
            };

            setCities((prev) => {
              const withoutCurrent = prev.filter((c) => !c.isCurrentLocation);
              const updated = [locationCity, ...withoutCurrent];
              saveCities(updated);
              return updated;
            });
            setSelectedCityId("current-location");
          } catch {}
        },
        () => {},
        { timeout: 5000 }
      );
    }
  }, []);

  const handleUpdateWeather = useCallback((cityId: string, data: WeatherData) => {
    setWeatherCache((prev) => ({ ...prev, [cityId]: data }));
  }, []);

  const handleSelectCity = useCallback((cityId: string) => {
    setSelectedCityId(cityId);
  }, []);

  const handleAddCity = useCallback((city: SavedCity) => {
    setCities((prev) => {
      if (prev.some((c) => c.id === city.id)) return prev;
      const updated = [...prev, city];
      saveCities(updated);
      return updated;
    });
    setSelectedCityId(city.id);
  }, []);

  const handleRemoveCity = useCallback((cityId: string) => {
    setCities((prev) => {
      const updated = prev.filter((c) => c.id !== cityId);
      saveCities(updated);
      // If we removed the selected city, select the first one
      if (cityId === selectedCityId && updated.length > 0) {
        setSelectedCityId(updated[0].id);
      }
      return updated;
    });
  }, [selectedCityId]);

  if (initialLoading) {
    return (
      <div className="h-screen w-screen bg-[#1c1c1e] flex items-center justify-center">
        <div className="text-white/40 text-lg">Loading...</div>
      </div>
    );
  }

  const selectedWeather = selectedCityId ? weatherCache[selectedCityId] : null;

  return (
    <div className="h-screen w-screen flex overflow-hidden">
      {/* Sidebar */}
      <Sidebar
        cities={cities}
        selectedCityId={selectedCityId}
        weatherCache={weatherCache}
        onSelectCity={handleSelectCity}
        onAddCity={handleAddCity}
        onRemoveCity={handleRemoveCity}
        onUpdateWeather={handleUpdateWeather}
      />

      {/* Main Content */}
      <main className="flex-1 h-full overflow-hidden">
        {selectedWeather ? (
          <WeatherDetail weather={selectedWeather} />
        ) : (
          <div className="h-full bg-clear-day flex items-center justify-center">
            <div className="text-white/40 text-lg">
              {cities.length === 0
                ? "Add a city to get started"
                : "Loading weather data..."}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
