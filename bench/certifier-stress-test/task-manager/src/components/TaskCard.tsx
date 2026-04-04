"use client";

interface Task {
  id: string;
  title: string;
  description: string;
  status: string;
  dueDate: string | null;
}

interface TaskCardProps {
  task: Task;
  onEdit: () => void;
  onDelete: () => void;
  onStatusChange: (status: string) => void;
}

const STATUS_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  TODO: { bg: "bg-yellow-50", text: "text-yellow-700", label: "To Do" },
  IN_PROGRESS: { bg: "bg-blue-50", text: "text-blue-700", label: "In Progress" },
  DONE: { bg: "bg-green-50", text: "text-green-700", label: "Done" },
};

const NEXT_STATUS: Record<string, string> = {
  TODO: "IN_PROGRESS",
  IN_PROGRESS: "DONE",
  DONE: "TODO",
};

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "No due date";
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function isOverdue(dateStr: string | null, status: string): boolean {
  if (!dateStr || status === "DONE") return false;
  return new Date(dateStr) < new Date(new Date().toDateString());
}

export function TaskCard({ task, onEdit, onDelete, onStatusChange }: TaskCardProps) {
  const style = STATUS_STYLES[task.status] || STATUS_STYLES.TODO;
  const overdue = isOverdue(task.dueDate, task.status);

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 hover:border-gray-300 transition-colors">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className={`font-medium ${task.status === "DONE" ? "line-through text-gray-400" : ""}`}>
              {task.title}
            </h3>
            <button
              onClick={() => onStatusChange(NEXT_STATUS[task.status])}
              className={`px-2 py-0.5 rounded-full text-xs font-medium ${style.bg} ${style.text} hover:opacity-80`}
              title={`Click to change status`}
            >
              {style.label}
            </button>
          </div>
          {task.description && (
            <p className="text-sm text-gray-500 mb-2">{task.description}</p>
          )}
          <p className={`text-xs ${overdue ? "text-red-600 font-medium" : "text-gray-400"}`}>
            {overdue ? "Overdue: " : "Due: "}
            {formatDate(task.dueDate)}
          </p>
        </div>
        <div className="flex gap-1 shrink-0">
          <button
            onClick={onEdit}
            className="p-1.5 text-gray-400 hover:text-gray-600 rounded"
            title="Edit"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
              <path d="m15 5 4 4" />
            </svg>
          </button>
          <button
            onClick={onDelete}
            className="p-1.5 text-gray-400 hover:text-red-600 rounded"
            title="Delete"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 6h18" />
              <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
              <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
