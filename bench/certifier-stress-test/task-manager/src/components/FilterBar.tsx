"use client";

const STATUSES = [
  { value: "", label: "All" },
  { value: "TODO", label: "To Do" },
  { value: "IN_PROGRESS", label: "In Progress" },
  { value: "DONE", label: "Done" },
];

interface FilterBarProps {
  statusFilter: string;
  onStatusChange: (status: string) => void;
  sortOrder: "asc" | "desc";
  onSortChange: (order: "asc" | "desc") => void;
}

export function FilterBar({ statusFilter, onStatusChange, sortOrder, onSortChange }: FilterBarProps) {
  return (
    <div className="flex items-center gap-4 mb-6">
      <div className="flex items-center gap-2">
        <span className="text-sm text-gray-500">Status:</span>
        <div className="flex gap-1">
          {STATUSES.map((s) => (
            <button
              key={s.value}
              onClick={() => onStatusChange(s.value)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                statusFilter === s.value
                  ? "bg-gray-900 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-2 ml-auto">
        <span className="text-sm text-gray-500">Due date:</span>
        <button
          onClick={() => onSortChange(sortOrder === "asc" ? "desc" : "asc")}
          className="px-3 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-600 hover:bg-gray-200"
        >
          {sortOrder === "asc" ? "Earliest first" : "Latest first"}
        </button>
      </div>
    </div>
  );
}
