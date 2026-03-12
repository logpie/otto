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
} from "lucide-react";

interface WeatherDetailProps {
  weather: WeatherData;
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

function getTempRange(daily: WeatherData["daily"]): { min: number; max: number } {
  const allLows = daily.map((d) => d.tempLow);
  const allHighs = daily.map((d) => d.tempHigh);
  return {
    min: Math.min(...allLows),
    max: Math.max(...allHighs),
  };
}

export default function WeatherDetail({ weather }: WeatherDetailProps) {
  const bgClass = getBackgroundClass(
    weather.current.condition.code,
    weather.current.isDay
  );

  const tempRange = getTempRange(weather.daily);
  const uvInfo = getUVLevel(weather.current.uvIndex);
  const todayData = weather.daily[0];

  return (
    <div className={`h-full w-full ${bgClass} weather-transition relative`}>
      <div className="main-scroll h-full">
        {/* Hero section */}
        <div className="flex flex-col items-center pt-12 pb-2 px-6">
          <h1 className="text-[34px] font-medium tracking-tight">{weather.location}</h1>
          <div className="text-[96px] font-thin leading-none tracking-tighter mt-1">
            {weather.current.temperature}°
          </div>
          <p className="text-[17px] font-medium opacity-90 mt-1">
            {weather.current.condition.description}
          </p>
          <p className="text-[15px] opacity-60 mt-0.5">
            H:{todayData?.tempHigh}°  L:{todayData?.tempLow}°
          </p>
        </div>

        <div className="px-6 pb-10 space-y-3 max-w-[580px] mx-auto">
          {/* Hourly Forecast */}
          <div className="glass p-4">
            <div className="flex items-center gap-1.5 mb-3 opacity-50">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <span className="text-[11px] font-semibold uppercase tracking-wider">Hourly Forecast</span>
            </div>
            <div className="hourly-scroll">
              <div className="flex gap-5 pb-1 min-w-max">
                {weather.hourly.map((hour, i) => {
                  const time = parseISO(hour.time);
                  const label = i === 0 ? "Now" : format(time, "ha").toLowerCase();
                  return (
                    <div key={i} className="flex flex-col items-center gap-1.5 min-w-[44px]">
                      <span className="text-[13px] font-medium opacity-80">{label}</span>
                      <div className="w-7 h-7 flex items-center justify-center">
                        {getWeatherIcon(hour.condition.icon, hour.isDay, 28)}
                      </div>
                      {hour.precipProbability > 10 && (
                        <span className="text-[11px] text-blue-300 font-medium leading-none">
                          {hour.precipProbability}%
                        </span>
                      )}
                      <span className="text-[15px] font-medium">{hour.temperature}°</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* 10-Day Forecast */}
          <div className="glass p-4">
            <div className="flex items-center gap-1.5 mb-2 opacity-50">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
                <line x1="16" y1="2" x2="16" y2="6" />
                <line x1="8" y1="2" x2="8" y2="6" />
                <line x1="3" y1="10" x2="21" y2="10" />
              </svg>
              <span className="text-[11px] font-semibold uppercase tracking-wider">10-Day Forecast</span>
            </div>
            <div className="divide-y divide-white/[0.08]">
              {weather.daily.map((day, i) => {
                const totalRange = tempRange.max - tempRange.min;
                const barLeft = totalRange > 0
                  ? ((day.tempLow - tempRange.min) / totalRange) * 100
                  : 0;
                const barWidth = totalRange > 0
                  ? ((day.tempHigh - day.tempLow) / totalRange) * 100
                  : 100;
                const currentTempPos = totalRange > 0
                  ? ((weather.current.temperature - tempRange.min) / totalRange) * 100
                  : 50;
                const showCurrentDot = i === 0;

                return (
                  <div key={i} className="flex items-center gap-2 py-2">
                    <span className="text-[14px] font-medium w-[52px] shrink-0 opacity-90">
                      {getDayLabel(day.date)}
                    </span>
                    <div className="w-7 h-7 flex items-center justify-center shrink-0">
                      {getWeatherIcon(day.condition.icon, true, 24)}
                    </div>
                    {day.precipProbability > 10 ? (
                      <span className="text-[11px] text-blue-300 font-medium w-7 text-right shrink-0">
                        {day.precipProbability}%
                      </span>
                    ) : (
                      <span className="w-7 shrink-0" />
                    )}
                    <span className="text-[14px] opacity-40 w-8 text-right shrink-0 tabular-nums">
                      {day.tempLow}°
                    </span>
                    <div className="flex-1 h-[5px] rounded-full bg-white/[0.08] relative mx-1.5">
                      <div
                        className="absolute h-full rounded-full"
                        style={{
                          left: `${barLeft}%`,
                          width: `${barWidth}%`,
                          background: "linear-gradient(to right, #60a5fa, #34d399, #fbbf24, #f97316)",
                        }}
                      />
                      {showCurrentDot && (
                        <div
                          className="absolute top-1/2 w-[9px] h-[9px] rounded-full bg-white shadow-sm"
                          style={{
                            left: `${Math.min(Math.max(currentTempPos, 3), 97)}%`,
                            transform: "translate(-50%, -50%)",
                          }}
                        />
                      )}
                    </div>
                    <span className="text-[14px] font-medium w-8 text-right shrink-0 tabular-nums">
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
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Sun size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">UV Index</span>
              </div>
              <div className="text-[28px] font-semibold leading-tight">{weather.current.uvIndex}</div>
              <div className="text-[14px] font-medium mt-0.5" style={{ color: uvInfo.color }}>
                {uvInfo.label}
              </div>
              <div className="uv-gradient mt-3 relative">
                <div
                  className="absolute -top-[5px] w-[11px] h-[11px] rounded-full bg-white border-2 shadow"
                  style={{
                    left: `${Math.min(weather.current.uvIndex / 11 * 100, 100)}%`,
                    borderColor: uvInfo.color,
                    transform: "translateX(-50%)",
                  }}
                />
              </div>
            </div>

            {/* Sunrise / Sunset */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Sunrise size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Sunrise</span>
              </div>
              {todayData && (
                <>
                  <div className="text-[28px] font-semibold leading-tight">
                    {format(parseISO(todayData.sunrise), "h:mm")}
                    <span className="text-[14px] ml-0.5 opacity-70">
                      {format(parseISO(todayData.sunrise), "a").toLowerCase()}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 mt-2 opacity-50">
                    <Sunset size={13} />
                    <span className="text-[12px]">
                      Sunset: {format(parseISO(todayData.sunset), "h:mm a").toLowerCase()}
                    </span>
                  </div>
                </>
              )}
            </div>

            {/* Wind */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Wind size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Wind</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-[28px] font-semibold leading-tight">{weather.current.windSpeed}</span>
                <span className="text-[13px] opacity-60">mph</span>
              </div>
              <div className="text-[12px] opacity-50 mt-1">
                {getWindDirection(weather.current.windDirection)} · Gusts {weather.current.windGust} mph
              </div>
            </div>

            {/* Feels Like */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Thermometer size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Feels Like</span>
              </div>
              <div className="text-[28px] font-semibold leading-tight">{weather.current.feelsLike}°</div>
              <div className="text-[12px] opacity-50 mt-1">
                {weather.current.feelsLike > weather.current.temperature
                  ? "Humidity is making it feel warmer."
                  : weather.current.feelsLike < weather.current.temperature
                  ? "Wind is making it feel cooler."
                  : "Similar to the actual temperature."}
              </div>
            </div>

            {/* Humidity */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Droplets size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Humidity</span>
              </div>
              <div className="text-[28px] font-semibold leading-tight">{weather.current.humidity}%</div>
              <div className="text-[12px] opacity-50 mt-1">
                The dew point is {weather.current.dewPoint}° right now.
              </div>
            </div>

            {/* Visibility */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Eye size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Visibility</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-[28px] font-semibold leading-tight">{weather.current.visibility}</span>
                <span className="text-[13px] opacity-60">mi</span>
              </div>
              <div className="text-[12px] opacity-50 mt-1">
                {weather.current.visibility >= 10
                  ? "It's perfectly clear right now."
                  : weather.current.visibility >= 5
                  ? "Visibility is moderate."
                  : "Visibility is low."}
              </div>
            </div>

            {/* Pressure */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Gauge size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Pressure</span>
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-[28px] font-semibold leading-tight">{weather.current.pressure}</span>
                <span className="text-[13px] opacity-60">hPa</span>
              </div>
              <div className="text-[12px] opacity-50 mt-1">
                {weather.current.pressure >= 1020
                  ? "High pressure system."
                  : weather.current.pressure <= 1000
                  ? "Low pressure system."
                  : "Normal atmospheric pressure."}
              </div>
            </div>

            {/* Precipitation */}
            <div className="glass detail-card p-4">
              <div className="flex items-center gap-1.5 mb-2 opacity-50">
                <Droplets size={13} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Precipitation</span>
              </div>
              <div className="text-[28px] font-semibold leading-tight">
                {todayData ? `${todayData.precipProbability}%` : "0%"}
              </div>
              <div className="text-[12px] opacity-50 mt-1">
                {todayData && todayData.precipProbability > 50
                  ? "Rain is likely today."
                  : todayData && todayData.precipProbability > 20
                  ? "Slight chance of rain."
                  : "No precipitation expected."}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
