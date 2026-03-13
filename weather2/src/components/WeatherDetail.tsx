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

function getWeatherSummary(weather: WeatherData): string {
  const current = weather.current;
  const today = weather.daily[0];
  const condition = current.condition.description.toLowerCase();
  const parts: string[] = [];

  if (condition.includes("clear") || condition.includes("sunny")) {
    parts.push(`Clear conditions expected throughout the day`);
  } else if (condition.includes("cloud") || condition.includes("overcast")) {
    parts.push(`Cloudy conditions expected throughout the day`);
  } else if (condition.includes("rain") || condition.includes("drizzle")) {
    parts.push(`Rain expected throughout the day`);
  } else if (condition.includes("snow")) {
    parts.push(`Snow expected throughout the day`);
  } else {
    parts.push(`${current.condition.description} expected`);
  }

  if (current.windGust > 15) {
    parts.push(`Wind gusts are up to ${current.windGust} mph`);
  }

  if (today && today.tempHigh !== undefined) {
    parts.push(`High of ${today.tempHigh}°`);
  }

  return parts.join(". ") + ".";
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
        {/* Hero section - Apple-style centered layout */}
        <div className="flex flex-col items-center pt-14 pb-1 px-6">
          <h1 className="text-[34px] font-medium tracking-tight leading-tight">{weather.location}</h1>
          <div className="text-[94px] font-extralight leading-none tracking-tighter mt-0">
            {weather.current.temperature}°
          </div>
          <p className="text-[17px] font-medium opacity-85 mt-0">
            {weather.current.condition.description}
          </p>
          <p className="text-[15px] opacity-50 mt-0.5 tabular-nums">
            H:{todayData?.tempHigh}°  L:{todayData?.tempLow}°
          </p>
        </div>

        {/* Weather summary text like Apple */}
        <div className="px-6 max-w-[580px] mx-auto mt-2 mb-1">
          <p className="text-[13px] text-white/50 leading-relaxed text-center">
            {getWeatherSummary(weather)}
          </p>
        </div>

        <div className="px-5 pb-12 space-y-2.5 max-w-[580px] mx-auto mt-2">
          {/* Hourly Forecast */}
          <div className="glass px-4 pt-3.5 pb-4">
            <div className="flex items-center gap-1.5 mb-3 pb-2.5 border-b border-white/[0.08]">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="opacity-40">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <span className="text-[11px] font-semibold uppercase tracking-wider opacity-40">Hourly Forecast</span>
            </div>
            <div className="hourly-scroll">
              <div className="flex gap-4 pb-0.5 min-w-max">
                {weather.hourly.map((hour, i) => {
                  const time = parseISO(hour.time);
                  const hourNum = time.getHours();
                  const isNewDay = i > 0 && hourNum === 0;
                  const label = i === 0 ? "Now" : isNewDay ? format(time, "EEE") : format(time, "ha").toLowerCase();
                  return (
                    <React.Fragment key={i}>
                      {isNewDay && (
                        <div className="flex items-stretch mx-0">
                          <div className="w-[0.5px] bg-white/15 my-2" />
                        </div>
                      )}
                      <div className="flex flex-col items-center gap-1.5 min-w-[44px]">
                        <span className={`text-[13px] font-medium ${i === 0 ? 'opacity-90' : isNewDay ? 'opacity-70' : 'opacity-55'}`}>{label}</span>
                        <div className="w-7 h-7 flex items-center justify-center my-0.5">
                          {getWeatherIcon(hour.condition.icon, hour.isDay, 28)}
                        </div>
                        {hour.precipProbability > 10 ? (
                          <span className="text-[11px] text-[#5ac8fa] font-medium leading-none">
                            {hour.precipProbability}%
                          </span>
                        ) : (
                          <span className="text-[11px] leading-none">&nbsp;</span>
                        )}
                        <span className={`text-[15px] font-medium ${i === 0 ? '' : 'opacity-90'}`}>{hour.temperature}°</span>
                      </div>
                    </React.Fragment>
                  );
                })}
              </div>
            </div>
          </div>

          {/* 10-Day Forecast */}
          <div className="glass px-4 pt-3.5 pb-2">
            <div className="flex items-center gap-1.5 mb-1.5 pb-2.5 border-b border-white/[0.08]">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="opacity-40">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
                <line x1="16" y1="2" x2="16" y2="6" />
                <line x1="8" y1="2" x2="8" y2="6" />
                <line x1="3" y1="10" x2="21" y2="10" />
              </svg>
              <span className="text-[11px] font-semibold uppercase tracking-wider opacity-40">10-Day Forecast</span>
            </div>
            <div>
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
                  <div key={i} className={`flex items-center gap-2 py-[9px] ${i < weather.daily.length - 1 ? 'border-b border-white/[0.06]' : ''}`}>
                    <span className={`text-[15px] w-[54px] shrink-0 ${i === 0 ? 'font-medium opacity-90' : 'font-normal opacity-55'}`}>
                      {getDayLabel(day.date)}
                    </span>
                    <div className="w-[28px] h-[28px] flex items-center justify-center shrink-0">
                      {getWeatherIcon(day.condition.icon, true, 24)}
                    </div>
                    {day.precipProbability > 10 ? (
                      <span className="text-[12px] text-[#5ac8fa] font-medium w-[30px] text-right shrink-0 tabular-nums">
                        {day.precipProbability}%
                      </span>
                    ) : (
                      <span className="w-[30px] shrink-0" />
                    )}
                    <span className="text-[15px] opacity-35 w-[32px] text-right shrink-0 tabular-nums font-normal">
                      {day.tempLow}°
                    </span>
                    <div className="flex-1 h-[5px] rounded-full bg-white/[0.06] relative mx-1.5">
                      <div
                        className="absolute h-full rounded-full"
                        style={{
                          left: `${barLeft}%`,
                          width: `${barWidth}%`,
                          background: "linear-gradient(to right, #5ac8fa, #4cd964, #ffd60a, #ff9500)",
                        }}
                      />
                      {showCurrentDot && (
                        <div
                          className="absolute top-1/2 w-[7px] h-[7px] rounded-full bg-white shadow-sm ring-[1.5px] ring-white/30"
                          style={{
                            left: `${Math.min(Math.max(currentTempPos, 3), 97)}%`,
                            transform: "translate(-50%, -50%)",
                          }}
                        />
                      )}
                    </div>
                    <span className="text-[15px] font-medium w-[32px] text-right shrink-0 tabular-nums opacity-90">
                      {day.tempHigh}°
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Detail Grid - 2 columns with Apple-like cards */}
          <div className="grid grid-cols-2 gap-2.5">
            {/* UV Index */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Sun size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">UV Index</span>
              </div>
              <div className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.uvIndex}</div>
              <div className="text-[15px] font-medium mt-1" style={{ color: uvInfo.color }}>
                {uvInfo.label}
              </div>
              <div className="uv-gradient mt-4 relative">
                <div
                  className="absolute -top-[4px] w-[10px] h-[10px] rounded-full bg-white shadow-md"
                  style={{
                    left: `${Math.min(weather.current.uvIndex / 11 * 100, 100)}%`,
                    transform: "translateX(-50%)",
                    boxShadow: "0 0 4px rgba(0,0,0,0.3)",
                  }}
                />
              </div>
            </div>

            {/* Sunrise / Sunset */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Sunrise size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Sunrise</span>
              </div>
              {todayData && (
                <>
                  <div className="text-[32px] font-semibold leading-none tracking-tight">
                    {format(parseISO(todayData.sunrise), "h:mm")}
                    <span className="text-[15px] ml-0.5 opacity-60 font-normal">
                      {format(parseISO(todayData.sunrise), "a").toLowerCase()}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 mt-4 opacity-40">
                    <Sunset size={12} strokeWidth={2.5} />
                    <span className="text-[12px]">
                      Sunset: {format(parseISO(todayData.sunset), "h:mm a").toLowerCase()}
                    </span>
                  </div>
                </>
              )}
            </div>

            {/* Wind */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Wind size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Wind</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.windSpeed}</span>
                <span className="text-[14px] opacity-50 font-normal">mph</span>
              </div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                {getWindDirection(weather.current.windDirection)} direction, gusts up to {weather.current.windGust} mph
              </div>
            </div>

            {/* Feels Like */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Thermometer size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Feels Like</span>
              </div>
              <div className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.feelsLike}°</div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                {weather.current.feelsLike > weather.current.temperature
                  ? "Humidity is making it feel warmer."
                  : weather.current.feelsLike < weather.current.temperature
                  ? "Wind is making it feel cooler."
                  : "Similar to the actual temperature."}
              </div>
            </div>

            {/* Humidity */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Droplets size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Humidity</span>
              </div>
              <div className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.humidity}%</div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                The dew point is {weather.current.dewPoint}° right now.
              </div>
            </div>

            {/* Visibility */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Eye size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Visibility</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.visibility}</span>
                <span className="text-[14px] opacity-50 font-normal">mi</span>
              </div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                {weather.current.visibility >= 10
                  ? "It's perfectly clear right now."
                  : weather.current.visibility >= 5
                  ? "Visibility is moderate."
                  : "Visibility is low."}
              </div>
            </div>

            {/* Pressure */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Gauge size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Pressure</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-[32px] font-semibold leading-none tracking-tight">{weather.current.pressure}</span>
                <span className="text-[14px] opacity-50 font-normal">hPa</span>
              </div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                {weather.current.pressure >= 1020
                  ? "High pressure system."
                  : weather.current.pressure <= 1000
                  ? "Low pressure system."
                  : "Normal atmospheric pressure."}
              </div>
            </div>

            {/* Precipitation */}
            <div className="glass detail-card p-4 pb-5">
              <div className="flex items-center gap-1.5 mb-3 opacity-40">
                <Droplets size={12} strokeWidth={2.5} />
                <span className="text-[11px] font-semibold uppercase tracking-wider">Precipitation</span>
              </div>
              <div className="text-[32px] font-semibold leading-none tracking-tight">
                {todayData ? `${todayData.precipProbability}%` : "0%"}
              </div>
              <div className="text-[13px] opacity-35 mt-2.5 leading-snug">
                {todayData && todayData.precipProbability > 50
                  ? "Rain is likely today."
                  : todayData && todayData.precipProbability > 20
                  ? "Slight chance of rain."
                  : "No precipitation expected."}
              </div>
            </div>
          </div>

          {/* Footer attribution */}
          <div className="text-center pt-4 pb-2">
            <p className="text-[11px] text-white/20">
              Weather data provided by Open-Meteo
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
