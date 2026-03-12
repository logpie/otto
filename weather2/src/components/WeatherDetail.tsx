"use client";

import React from "react";
import { WeatherData } from "@/lib/types";
import { getBackgroundClass } from "@/lib/weather-api";
import { getWeatherIcon } from "@/lib/weather-icons";
import { format, parseISO, isToday, isTomorrow } from "date-fns";
import {
  Wind,
  Droplets,
  Eye,
  Thermometer,
  Sun,
  Sunrise,
  Sunset,
  Gauge,
  ArrowLeft,
} from "lucide-react";

interface WeatherDetailProps {
  weather: WeatherData;
  onBack: () => void;
}

function getWindDirection(degrees: number): string {
  const dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return dirs[Math.round(degrees / 22.5) % 16];
}

function getUVLevel(index: number): { label: string; color: string } {
  if (index <= 2) return { label: "Low", color: "#4ade80" };
  if (index <= 5) return { label: "Moderate", color: "#facc15" };
  if (index <= 7) return { label: "High", color: "#f97316" };
  if (index <= 10) return { label: "Very High", color: "#ef4444" };
  return { label: "Extreme", color: "#a855f7" };
}

function getDayLabel(dateStr: string): string {
  const date = parseISO(dateStr);
  if (isToday(date)) return "Today";
  if (isTomorrow(date)) return "Tomorrow";
  return format(date, "EEE");
}

// Get the global temp range across all daily forecasts for the bar visualization
function getTempRange(daily: WeatherData["daily"]): { min: number; max: number } {
  const allLows = daily.map((d) => d.tempLow);
  const allHighs = daily.map((d) => d.tempHigh);
  return {
    min: Math.min(...allLows),
    max: Math.max(...allHighs),
  };
}

export default function WeatherDetail({ weather, onBack }: WeatherDetailProps) {
  const bgClass = getBackgroundClass(
    weather.current.condition.code,
    weather.current.isDay
  );

  const tempRange = getTempRange(weather.daily);
  const uvInfo = getUVLevel(weather.current.uvIndex);
  const todayData = weather.daily[0];

  return (
    <div className={`h-full w-full ${bgClass} relative`}>
      {/* Back button */}
      <button
        onClick={onBack}
        className="absolute top-4 left-4 z-20 p-2 rounded-full bg-white/10 backdrop-blur-md hover:bg-white/20 transition-colors"
        aria-label="Back to city list"
      >
        <ArrowLeft size={20} color="white" />
      </button>

      <div className="main-scroll h-full pt-2 pb-8">
        {/* Hero section */}
        <div className="flex flex-col items-center pt-14 pb-4 px-4">
          <h1 className="text-3xl font-medium tracking-tight">{weather.location}</h1>
          <div className="text-8xl font-thin mt-1 tracking-tighter">
            {weather.current.temperature}°
          </div>
          <p className="text-lg font-medium opacity-90 mt-1">
            {weather.current.condition.description}
          </p>
          <p className="text-base opacity-75 mt-0.5">
            H:{todayData?.tempHigh}° L:{todayData?.tempLow}°
          </p>
        </div>

        <div className="px-4 space-y-3 max-w-lg mx-auto">
          {/* Hourly Forecast */}
          <div className="glass p-4">
            <div className="flex items-center gap-1.5 mb-3 opacity-60">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <span className="text-xs font-semibold uppercase tracking-wider">Hourly Forecast</span>
            </div>
            <div className="hourly-scroll">
              <div className="flex gap-5 pb-1 min-w-max">
                {weather.hourly.map((hour, i) => {
                  const time = parseISO(hour.time);
                  const label = i === 0 ? "Now" : format(time, "ha").toLowerCase();
                  return (
                    <div key={i} className="flex flex-col items-center gap-1.5 min-w-[44px]">
                      <span className="text-sm font-medium">{label}</span>
                      <div className="w-8 h-8 flex items-center justify-center">
                        {getWeatherIcon(hour.condition.icon, hour.isDay, 32)}
                      </div>
                      {hour.precipProbability > 10 && (
                        <span className="text-xs text-blue-300 font-medium">
                          {hour.precipProbability}%
                        </span>
                      )}
                      <span className="text-base font-medium">{hour.temperature}°</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* 10-Day Forecast */}
          <div className="glass p-4">
            <div className="flex items-center gap-1.5 mb-2 opacity-60">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
                <line x1="16" y1="2" x2="16" y2="6" />
                <line x1="8" y1="2" x2="8" y2="6" />
                <line x1="3" y1="10" x2="21" y2="10" />
              </svg>
              <span className="text-xs font-semibold uppercase tracking-wider">10-Day Forecast</span>
            </div>
            <div className="divide-y divide-white/10">
              {weather.daily.map((day, i) => {
                const totalRange = tempRange.max - tempRange.min;
                const barLeft = totalRange > 0
                  ? ((day.tempLow - tempRange.min) / totalRange) * 100
                  : 0;
                const barWidth = totalRange > 0
                  ? ((day.tempHigh - day.tempLow) / totalRange) * 100
                  : 100;

                // Current temp indicator position
                const currentTempPos = totalRange > 0
                  ? ((weather.current.temperature - tempRange.min) / totalRange) * 100
                  : 50;
                const showCurrentDot = i === 0;

                return (
                  <div key={i} className="flex items-center gap-2 py-2.5">
                    <span className="text-base font-medium w-12 shrink-0">
                      {getDayLabel(day.date)}
                    </span>
                    <div className="w-8 h-8 flex items-center justify-center shrink-0">
                      {getWeatherIcon(day.condition.icon, true, 28)}
                    </div>
                    {day.precipProbability > 10 ? (
                      <span className="text-xs text-blue-300 font-medium w-8 text-right shrink-0">
                        {day.precipProbability}%
                      </span>
                    ) : (
                      <span className="w-8 shrink-0" />
                    )}
                    <span className="text-base opacity-50 w-8 text-right shrink-0">
                      {day.tempLow}°
                    </span>
                    <div className="flex-1 h-1.5 rounded-full bg-white/10 relative mx-1">
                      <div
                        className="absolute h-full rounded-full"
                        style={{
                          left: `${barLeft}%`,
                          width: `${barWidth}%`,
                          background: `linear-gradient(to right, #60a5fa, #34d399, #fbbf24, #f97316)`,
                        }}
                      />
                      {showCurrentDot && (
                        <div
                          className="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-white border-2 border-white/80 shadow-sm"
                          style={{ left: `${Math.min(Math.max(currentTempPos, 2), 98)}%`, transform: "translate(-50%, -50%)" }}
                        />
                      )}
                    </div>
                    <span className="text-base font-medium w-8 text-right shrink-0">
                      {day.tempHigh}°
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Detail Grid */}
          <div className="grid grid-cols-2 gap-3">
            {/* UV Index */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Sun size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">UV Index</span>
              </div>
              <div className="text-3xl font-semibold">{weather.current.uvIndex}</div>
              <div className="font-medium mt-0.5" style={{ color: uvInfo.color }}>
                {uvInfo.label}
              </div>
              <div className="uv-gradient mt-3 relative">
                <div
                  className="absolute -top-1 w-3 h-3 rounded-full bg-white border-2 shadow"
                  style={{
                    left: `${Math.min(weather.current.uvIndex / 11 * 100, 100)}%`,
                    borderColor: uvInfo.color,
                    transform: "translateX(-50%)",
                  }}
                />
              </div>
            </div>

            {/* Sunrise / Sunset */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Sunrise size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Sunrise</span>
              </div>
              {todayData && (
                <>
                  <div className="text-3xl font-semibold">
                    {format(parseISO(todayData.sunrise), "h:mm")}
                    <span className="text-lg ml-0.5">{format(parseISO(todayData.sunrise), "a").toLowerCase()}</span>
                  </div>
                  <div className="flex items-center gap-1 mt-2 opacity-60">
                    <Sunset size={14} />
                    <span className="text-sm">
                      Sunset: {format(parseISO(todayData.sunset), "h:mm a").toLowerCase()}
                    </span>
                  </div>
                </>
              )}
            </div>

            {/* Wind */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Wind size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Wind</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-3xl font-semibold">{weather.current.windSpeed}</span>
                <span className="text-base opacity-70">mph</span>
              </div>
              <div className="text-sm opacity-70 mt-1">
                {getWindDirection(weather.current.windDirection)} · Gusts {weather.current.windGust} mph
              </div>
            </div>

            {/* Feels Like */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Thermometer size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Feels Like</span>
              </div>
              <div className="text-3xl font-semibold">{weather.current.feelsLike}°</div>
              <div className="text-sm opacity-70 mt-1">
                {weather.current.feelsLike > weather.current.temperature
                  ? "Humidity is making it feel warmer"
                  : weather.current.feelsLike < weather.current.temperature
                  ? "Wind is making it feel cooler"
                  : "Similar to the actual temperature"}
              </div>
            </div>

            {/* Humidity */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Droplets size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Humidity</span>
              </div>
              <div className="text-3xl font-semibold">{weather.current.humidity}%</div>
              <div className="text-sm opacity-70 mt-1">
                The dew point is {weather.current.dewPoint}° right now.
              </div>
            </div>

            {/* Visibility */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Eye size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Visibility</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-3xl font-semibold">{weather.current.visibility}</span>
                <span className="text-base opacity-70">mi</span>
              </div>
              <div className="text-sm opacity-70 mt-1">
                {weather.current.visibility >= 10
                  ? "It's perfectly clear right now."
                  : weather.current.visibility >= 5
                  ? "Visibility is moderate."
                  : "Visibility is low."}
              </div>
            </div>

            {/* Pressure */}
            <div className="glass p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-60">
                <Gauge size={14} />
                <span className="text-xs font-semibold uppercase tracking-wider">Pressure</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-3xl font-semibold">{weather.current.pressure}</span>
                <span className="text-base opacity-70">hPa</span>
              </div>
            </div>
          </div>
        </div>

        {/* Footer spacer */}
        <div className="h-8" />
      </div>
    </div>
  );
}
