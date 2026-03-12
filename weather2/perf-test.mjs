// Performance test for geocoding and weather API calls
// Verifies both complete under 300ms with warm (keep-alive) connections.
//
// In a browser/Electron, HTTP keep-alive reuses TCP+TLS connections automatically.
// This test warms connections first, then measures subsequent requests that
// reuse the established connection (same as real app behavior).

import https from "node:https";

// Keep-alive agent to reuse TCP+TLS connections (mirrors browser behavior)
const agent = new https.Agent({ keepAlive: true, keepAliveMsecs: 30000, maxSockets: 4 });

function fetchWithKeepAlive(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { agent }, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve(JSON.parse(data)));
    });
    req.on("error", reject);
  });
}

const GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search";
const WEATHER_URL = "https://api.open-meteo.com/v1/forecast";

async function warmConnections() {
  console.log("Warming connections (TCP + TLS handshake)...");
  await Promise.all([
    fetchWithKeepAlive(`${GEOCODING_URL}?name=a&count=1&language=en&format=json`),
    fetchWithKeepAlive(`${WEATHER_URL}?latitude=0&longitude=0&current=temperature_2m&timezone=auto&forecast_days=1`),
  ]);
  console.log("Connections warm (keep-alive active).\n");
}

async function testGeocoding() {
  const start = performance.now();
  const data = await fetchWithKeepAlive(
    `${GEOCODING_URL}?name=${encodeURIComponent("New York")}&count=5&language=en&format=json`
  );
  const elapsed = performance.now() - start;
  const pass = elapsed < 300;
  console.log(`  Geocoding: ${elapsed.toFixed(1)}ms ${pass ? "✅ PASS" : "❌ FAIL"} (${data.results?.length || 0} results)`);
  return elapsed;
}

async function testFetchWeather() {
  const params = new URLSearchParams({
    latitude: "40.7128",
    longitude: "-74.006",
    current: "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index,visibility,surface_pressure,dew_point_2m,is_day",
    hourly: "temperature_2m,weather_code,precipitation_probability,is_day",
    daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max,wind_speed_10m_max",
    temperature_unit: "fahrenheit",
    wind_speed_unit: "mph",
    precipitation_unit: "inch",
    timezone: "auto",
    forecast_days: "7",
    forecast_hours: "26",
  });

  const start = performance.now();
  const data = await fetchWithKeepAlive(`${WEATHER_URL}?${params}`);
  const elapsed = performance.now() - start;
  const pass = elapsed < 300;
  console.log(`  Weather:   ${elapsed.toFixed(1)}ms ${pass ? "✅ PASS" : "❌ FAIL"} (temp: ${data.current?.temperature_2m}°F)`);
  return elapsed;
}

async function main() {
  const trials = 5;

  // Step 1: Warm connections (simulates app's warmConnections() on module load)
  await warmConnections();

  // Step 2: Run trials with warm connections
  const geoTimes = [];
  const weatherTimes = [];

  for (let i = 0; i < trials; i++) {
    console.log(`Trial ${i + 1}:`);
    const geo = await testGeocoding();
    const weather = await testFetchWeather();
    geoTimes.push(geo);
    weatherTimes.push(weather);
    console.log();
  }

  // Step 3: Report results (use median to account for network jitter)
  geoTimes.sort((a, b) => a - b);
  weatherTimes.sort((a, b) => a - b);
  const medianGeo = geoTimes[Math.floor(trials / 2)];
  const medianWeather = weatherTimes[Math.floor(trials / 2)];

  console.log("=== Results (warm connection, median of 5 trials) ===");
  console.log(`Geocoding median: ${medianGeo.toFixed(1)}ms ${medianGeo < 300 ? "✅" : "❌"}`);
  console.log(`Weather median:   ${medianWeather.toFixed(1)}ms ${medianWeather < 300 ? "✅" : "❌"}`);

  const pass = medianGeo < 300 && medianWeather < 300;
  console.log(`\nOverall: ${pass ? "✅ PASS" : "❌ FAIL"}`);

  agent.destroy();
  process.exit(pass ? 0 : 1);
}

main().catch(console.error);
