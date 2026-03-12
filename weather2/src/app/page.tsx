"use client";

import React, { useState, useEffect, useCallback } from "react";
import { SavedCity, WeatherData } from "@/lib/types";
import CityList from "@/components/CityList";
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
  const [selectedWeather, setSelectedWeather] = useState<WeatherData | null>(null);
  const [view, setView] = useState<"list" | "detail">("list");
  const [initialLoading, setInitialLoading] = useState(true);

  // Load cities from localStorage
  useEffect(() => {
    const loaded = loadCities();
    setCities(loaded);

    // Try to get user's location
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
          } catch {}
        },
        () => {
          // Geolocation denied — use defaults
        },
        { timeout: 5000 }
      );
    }

    setInitialLoading(false);
  }, []);

  const handleSelectCity = useCallback((weather: WeatherData) => {
    setSelectedWeather(weather);
    setView("detail");
  }, []);

  const handleAddCity = useCallback((city: SavedCity) => {
    setCities((prev) => {
      if (prev.some((c) => c.id === city.id)) return prev;
      const updated = [...prev, city];
      saveCities(updated);
      return updated;
    });
  }, []);

  const handleRemoveCity = useCallback((cityId: string) => {
    setCities((prev) => {
      const updated = prev.filter((c) => c.id !== cityId);
      saveCities(updated);
      return updated;
    });
  }, []);

  const handleBack = useCallback(() => {
    setView("list");
    setSelectedWeather(null);
  }, []);

  if (initialLoading) {
    return (
      <div className="h-screen w-screen bg-gray-900 flex items-center justify-center">
        <div className="text-white/50 text-lg">Loading...</div>
      </div>
    );
  }

  return (
    <div className="h-screen w-screen overflow-hidden">
      {view === "detail" && selectedWeather ? (
        <WeatherDetail weather={selectedWeather} onBack={handleBack} />
      ) : (
        <CityList
          cities={cities}
          onSelectCity={handleSelectCity}
          onAddCity={handleAddCity}
          onRemoveCity={handleRemoveCity}
        />
      )}
    </div>
  );
}
