export interface WeatherCondition {
  code: number;
  description: string;
  icon: string;
}

export interface CurrentWeather {
  temperature: number;
  feelsLike: number;
  humidity: number;
  windSpeed: number;
  windDirection: number;
  windGust: number;
  uvIndex: number;
  visibility: number;
  pressure: number;
  dewPoint: number;
  condition: WeatherCondition;
  isDay: boolean;
}

export interface HourlyForecast {
  time: string;
  temperature: number;
  condition: WeatherCondition;
  precipProbability: number;
  isDay: boolean;
}

export interface DailyForecast {
  date: string;
  tempHigh: number;
  tempLow: number;
  condition: WeatherCondition;
  precipProbability: number;
  sunrise: string;
  sunset: string;
  uvIndexMax: number;
  windSpeedMax: number;
}

export interface WeatherData {
  location: string;
  region: string;
  country: string;
  latitude: number;
  longitude: number;
  timezone: string;
  current: CurrentWeather;
  hourly: HourlyForecast[];
  daily: DailyForecast[];
}

export interface GeoLocation {
  name: string;
  region: string;
  country: string;
  latitude: number;
  longitude: number;
}

export interface SavedCity {
  id: string;
  name: string;
  region: string;
  country: string;
  latitude: number;
  longitude: number;
  isCurrentLocation?: boolean;
}

export interface StageTiming {
  label: string;
  startTime: number;   // wallclock ms since epoch
  duration: number;     // elapsed ms
}

export interface FetchTimings {
  cityId: string;
  cityName: string;
  stages: StageTiming[];
  totalDuration: number;
  fetchedAt: Date;
}
