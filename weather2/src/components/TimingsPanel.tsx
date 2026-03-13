import React, { useState, useEffect } from "react";
import { FetchTimings, StageTiming } from "@/lib/types";
import { warmConnectionTiming } from "@/lib/weather-api";
import { Timer } from "lucide-react";

interface TimingsPanelProps {
  timingsCache: Record<string, FetchTimings>;
  selectedCityId: string | null;
}

function formatTime(ms: number): string {
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatWallclock(date: Date): string {
  const hms = date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const ms = String(date.getMilliseconds()).padStart(3, "0");
  return `${hms}.${ms}`;
}

function formatStageWallclock(startTime: number): string {
  const d = new Date(startTime);
  const hms = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hms}.${ms}`;
}

function StageRow({
  stage,
  timelineStart,
  timelineSpan,
}: {
  stage: StageTiming;
  timelineStart: number;
  timelineSpan: number;
}) {
  const offsetPct = timelineSpan > 0 ? ((stage.startTime - timelineStart) / timelineSpan) * 100 : 0;
  const widthPct = timelineSpan > 0 ? (stage.duration / timelineSpan) * 100 : 0;

  return (
    <div className="flex items-center gap-2 py-1">
      <span className="text-[10px] text-white/40 w-[80px] shrink-0 tabular-nums text-right font-mono">
        {formatStageWallclock(stage.startTime)}
      </span>
      <span className="text-[11px] text-white/70 w-[140px] shrink-0 truncate">
        {stage.label}
      </span>
      <div className="flex-1 h-[6px] rounded-full bg-white/[0.06] relative overflow-hidden">
        <div
          className="absolute top-0 h-full rounded-full bg-blue-400/60"
          style={{
            left: `${offsetPct}%`,
            width: `${Math.max(widthPct, 1)}%`,
          }}
        />
      </div>
      <span className="text-[11px] text-white/50 w-[50px] shrink-0 text-right tabular-nums font-mono">
        {formatTime(stage.duration)}
      </span>
    </div>
  );
}

export default function TimingsPanel({ timingsCache, selectedCityId }: TimingsPanelProps) {
  const [collapsed, setCollapsed] = useState(true);
  const [warmTiming, setWarmTiming] = useState<StageTiming | null>(null);

  // Poll for warm connection timing (it's set asynchronously)
  useEffect(() => {
    const check = () => {
      if (warmConnectionTiming) {
        setWarmTiming(warmConnectionTiming);
      }
    };
    check();
    const interval = setInterval(check, 200);
    return () => clearInterval(interval);
  }, []);

  const allTimings = Object.values(timingsCache);
  const selectedTimings = selectedCityId ? timingsCache[selectedCityId] : null;

  if (allTimings.length === 0 && !warmTiming) return null;

  // Collect all stages for the selected city (or the first available)
  const displayTimings = selectedTimings || allTimings[0];
  const allStages: StageTiming[] = [];

  if (warmTiming) {
    allStages.push(warmTiming);
  }
  if (displayTimings) {
    allStages.push(...displayTimings.stages);
  }

  // Compute timeline span for waterfall positioning
  const timelineStart = allStages.length > 0 ? Math.min(...allStages.map((s) => s.startTime)) : 0;
  const timelineEnd = allStages.length > 0
    ? Math.max(...allStages.map((s) => s.startTime + s.duration))
    : 1;
  const timelineSpan = Math.max(timelineEnd - timelineStart, 1);

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50">
      {/* Toggle bar */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center justify-between px-4 py-1.5 bg-[#1c1c1e]/95 backdrop-blur-xl border-t border-white/[0.08] hover:bg-[#2c2c2e]/95 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Timer size={12} className="text-blue-400/70" />
          <span className="text-[11px] text-white/50 font-medium">
            Stage Timings
          </span>
          {displayTimings && (
            <span className="text-[10px] text-white/30 font-mono tabular-nums">
              Total: {formatTime(displayTimings.totalDuration)}
              {" | "}
              Fetched: {formatWallclock(displayTimings.fetchedAt)}
            </span>
          )}
        </div>
        <span className="text-[10px] text-white/30">
          {collapsed ? "▲" : "▼"}
        </span>
      </button>

      {/* Expanded panel */}
      {!collapsed && (
        <div className="bg-[#1c1c1e]/98 backdrop-blur-xl border-t border-white/[0.06] px-4 py-3 max-h-[300px] overflow-y-auto">
          {/* City tabs */}
          {allTimings.length > 1 && (
            <div className="flex gap-1 mb-3 overflow-x-auto pb-1">
              {allTimings.map((t) => (
                <span
                  key={t.cityId}
                  className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${
                    t.cityId === displayTimings?.cityId
                      ? "bg-blue-500/20 text-blue-300"
                      : "bg-white/[0.06] text-white/30"
                  }`}
                >
                  {t.cityName}
                </span>
              ))}
            </div>
          )}

          {/* Waterfall */}
          <div className="space-y-0">
            {allStages.map((stage, i) => (
              <StageRow
                key={i}
                stage={stage}
                timelineStart={timelineStart}
                timelineSpan={timelineSpan}
              />
            ))}
          </div>

          {/* Summary for all cities */}
          {allTimings.length > 0 && (
            <div className="mt-3 pt-2 border-t border-white/[0.06]">
              <div className="text-[10px] text-white/30 font-medium mb-1">All Cities</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
                {allTimings.map((t) => (
                  <div key={t.cityId} className="flex justify-between">
                    <span className="text-[10px] text-white/40 truncate">{t.cityName}</span>
                    <span className="text-[10px] text-white/50 font-mono tabular-nums ml-2">
                      {formatTime(t.totalDuration)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
