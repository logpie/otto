import React from "react";

interface IconProps {
  size?: number;
  className?: string;
}

export function ClearDayIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <circle cx="50" cy="50" r="20" fill="#FFD700" />
      <g stroke="#FFD700" strokeWidth="3" strokeLinecap="round">
        {[0, 45, 90, 135, 180, 225, 270, 315].map((angle) => {
          const rad = (angle * Math.PI) / 180;
          const x1 = 50 + 28 * Math.cos(rad);
          const y1 = 50 + 28 * Math.sin(rad);
          const x2 = 50 + 36 * Math.cos(rad);
          const y2 = 50 + 36 * Math.sin(rad);
          return <line key={angle} x1={x1} y1={y1} x2={x2} y2={y2} />;
        })}
      </g>
    </svg>
  );
}

export function ClearNightIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M60 20 C40 20, 25 40, 30 58 C35 76, 55 85, 70 78 C58 82, 40 75, 35 60 C30 45, 38 28, 55 22 Z"
        fill="#E8E8E8"
      />
      <circle cx="72" cy="28" r="2" fill="#E8E8E8" opacity="0.6" />
      <circle cx="82" cy="42" r="1.5" fill="#E8E8E8" opacity="0.5" />
      <circle cx="78" cy="55" r="1" fill="#E8E8E8" opacity="0.4" />
    </svg>
  );
}

export function PartlyCloudyDayIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <circle cx="40" cy="35" r="14" fill="#FFD700" />
      <g stroke="#FFD700" strokeWidth="2" strokeLinecap="round">
        {[0, 60, 120, 180, 240, 300].map((angle) => {
          const rad = (angle * Math.PI) / 180;
          const x1 = 40 + 19 * Math.cos(rad);
          const y1 = 35 + 19 * Math.sin(rad);
          const x2 = 40 + 24 * Math.cos(rad);
          const y2 = 35 + 24 * Math.sin(rad);
          return <line key={angle} x1={x1} y1={y1} x2={x2} y2={y2} />;
        })}
      </g>
      <path
        d="M30 72 C30 72, 30 58, 45 58 C45 50, 55 45, 63 50 C70 45, 82 48, 82 58 C90 58, 90 72, 82 72 Z"
        fill="white"
        opacity="0.95"
      />
    </svg>
  );
}

export function PartlyCloudyNightIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M45 22 C33 22, 24 34, 28 44 C25 45, 25 45, 28 44 C36 26, 50 22, 45 22 Z"
        fill="#C4C4C4"
      />
      <path
        d="M30 72 C30 72, 30 58, 45 58 C45 50, 55 45, 63 50 C70 45, 82 48, 82 58 C90 58, 90 72, 82 72 Z"
        fill="#C4C4C4"
        opacity="0.9"
      />
    </svg>
  );
}

export function CloudyIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M22 68 C22 68, 22 54, 37 54 C37 46, 47 42, 55 46 C60 40, 72 42, 74 52 C82 52, 84 64, 78 68 Z"
        fill="#B0B0B0"
        opacity="0.6"
      />
      <path
        d="M18 78 C18 78, 18 62, 35 62 C35 53, 47 48, 56 53 C63 47, 76 50, 78 60 C87 60, 88 74, 82 78 Z"
        fill="white"
        opacity="0.95"
      />
    </svg>
  );
}

export function RainIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M20 55 C20 55, 20 42, 35 42 C35 34, 45 30, 55 34 C60 28, 74 30, 76 40 C84 40, 86 52, 80 55 Z"
        fill="#A0A0A0"
        opacity="0.9"
      />
      <line x1="32" y1="62" x2="28" y2="76" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="48" y1="62" x2="44" y2="76" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="64" y1="62" x2="60" y2="76" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function HeavyRainIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M20 50 C20 50, 20 37, 35 37 C35 29, 45 25, 55 29 C60 23, 74 25, 76 35 C84 35, 86 47, 80 50 Z"
        fill="#808080"
        opacity="0.9"
      />
      <line x1="28" y1="57" x2="22" y2="75" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="40" y1="57" x2="34" y2="75" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="52" y1="57" x2="46" y2="75" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="64" y1="57" x2="58" y2="75" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="76" y1="57" x2="70" y2="75" stroke="#5AC8FA" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function SnowIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M20 55 C20 55, 20 42, 35 42 C35 34, 45 30, 55 34 C60 28, 74 30, 76 40 C84 40, 86 52, 80 55 Z"
        fill="#C8D0D8"
        opacity="0.9"
      />
      <circle cx="32" cy="66" r="3" fill="white" opacity="0.9" />
      <circle cx="50" cy="70" r="3" fill="white" opacity="0.9" />
      <circle cx="68" cy="66" r="3" fill="white" opacity="0.9" />
      <circle cx="40" cy="80" r="2.5" fill="white" opacity="0.7" />
      <circle cx="58" cy="82" r="2.5" fill="white" opacity="0.7" />
    </svg>
  );
}

export function ThunderstormIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M20 48 C20 48, 20 35, 35 35 C35 27, 45 23, 55 27 C60 21, 74 23, 76 33 C84 33, 86 45, 80 48 Z"
        fill="#666"
        opacity="0.9"
      />
      <polygon points="52,52 44,68 50,68 46,82 60,64 54,64 58,52" fill="#FFD700" />
      <line x1="30" y1="56" x2="26" y2="72" stroke="#5AC8FA" strokeWidth="2" strokeLinecap="round" />
      <line x1="72" y1="56" x2="68" y2="72" stroke="#5AC8FA" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export function FogIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <line x1="20" y1="40" x2="80" y2="40" stroke="white" strokeWidth="4" strokeLinecap="round" opacity="0.5" />
      <line x1="25" y1="52" x2="75" y2="52" stroke="white" strokeWidth="4" strokeLinecap="round" opacity="0.6" />
      <line x1="20" y1="64" x2="80" y2="64" stroke="white" strokeWidth="4" strokeLinecap="round" opacity="0.5" />
      <line x1="30" y1="76" x2="70" y2="76" stroke="white" strokeWidth="4" strokeLinecap="round" opacity="0.4" />
    </svg>
  );
}

export function DrizzleIcon({ size = 48, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className}>
      <path
        d="M20 55 C20 55, 20 42, 35 42 C35 34, 45 30, 55 34 C60 28, 74 30, 76 40 C84 40, 86 52, 80 55 Z"
        fill="#B0B0B0"
        opacity="0.9"
      />
      <circle cx="35" cy="66" r="2" fill="#5AC8FA" opacity="0.8" />
      <circle cx="50" cy="72" r="2" fill="#5AC8FA" opacity="0.8" />
      <circle cx="65" cy="66" r="2" fill="#5AC8FA" opacity="0.8" />
    </svg>
  );
}

export function getWeatherIcon(iconName: string, isDay: boolean, size?: number, className?: string) {
  const props = { size, className };

  switch (iconName) {
    case "clear":
      return isDay ? <ClearDayIcon {...props} /> : <ClearNightIcon {...props} />;
    case "mainly-clear":
      return isDay ? <ClearDayIcon {...props} /> : <ClearNightIcon {...props} />;
    case "partly-cloudy":
      return isDay ? <PartlyCloudyDayIcon {...props} /> : <PartlyCloudyNightIcon {...props} />;
    case "overcast":
      return <CloudyIcon {...props} />;
    case "fog":
      return <FogIcon {...props} />;
    case "drizzle":
    case "freezing-drizzle":
      return <DrizzleIcon {...props} />;
    case "rain":
    case "rain-showers":
    case "freezing-rain":
      return <RainIcon {...props} />;
    case "heavy-rain":
      return <HeavyRainIcon {...props} />;
    case "snow":
    case "heavy-snow":
      return <SnowIcon {...props} />;
    case "thunderstorm":
      return <ThunderstormIcon {...props} />;
    default:
      return isDay ? <ClearDayIcon {...props} /> : <ClearNightIcon {...props} />;
  }
}
