"use client";

import { useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useEffect, useState, useCallback } from "react";
import { TaskForm } from "@/components/TaskForm";
import { TaskCard } from "@/components/TaskCard";
import { FilterBar } from "@/components/FilterBar";

interface Task {
  id: string;
  title: string;
  description: string;
  status: string;
  dueDate: string | null;
  createdAt: string;
  updatedAt: string;
}

export default function HomePage() {
  const { data: session, status: authStatus } = useSession();
  const router = useRouter();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("asc");
  const [showForm, setShowForm] = useState(false);
  const [editingTask, setEditingTask] = useState<Task | null>(null);

  const fetchTasks = useCallback(async () => {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    params.set("sort", "dueDate");
    params.set("order", sortOrder);

    const res = await fetch(`/api/tasks?${params}`);
    if (res.ok) {
      setTasks(await res.json());
    }
    setLoading(false);
  }, [statusFilter, sortOrder]);

  useEffect(() => {
    if (authStatus === "unauthenticated") {
      router.push("/login");
    } else if (authStatus === "authenticated") {
      fetchTasks();
    }
  }, [authStatus, router, fetchTasks]);

  if (authStatus === "loading" || authStatus === "unauthenticated") {
    return <div className="text-center py-16 text-gray-500">Loading...</div>;
  }

  async function handleCreate(data: { title: string; description: string; status: string; dueDate: string }) {
    const res = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      setShowForm(false);
      fetchTasks();
    }
  }

  async function handleUpdate(data: { title: string; description: string; status: string; dueDate: string }) {
    if (!editingTask) return;
    const res = await fetch(`/api/tasks/${editingTask.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      setEditingTask(null);
      fetchTasks();
    }
  }

  async function handleDelete(id: string) {
    if (!confirm("Delete this task?")) return;
    const res = await fetch(`/api/tasks/${id}`, { method: "DELETE" });
    if (res.ok) fetchTasks();
  }

  async function handleStatusChange(id: string, status: string) {
    const res = await fetch(`/api/tasks/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    if (res.ok) fetchTasks();
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">My Tasks</h1>
        <button
          onClick={() => { setShowForm(true); setEditingTask(null); }}
          className="bg-gray-900 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-gray-800"
        >
          New Task
        </button>
      </div>

      {(showForm || editingTask) && (
        <div className="mb-6">
          <TaskForm
            task={editingTask}
            onSubmit={editingTask ? handleUpdate : handleCreate}
            onCancel={() => { setShowForm(false); setEditingTask(null); }}
          />
        </div>
      )}

      <FilterBar
        statusFilter={statusFilter}
        onStatusChange={setStatusFilter}
        sortOrder={sortOrder}
        onSortChange={setSortOrder}
      />

      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading tasks...</div>
      ) : tasks.length === 0 ? (
        <div className="text-center py-12 text-gray-400">
          {statusFilter ? "No tasks with this status." : "No tasks yet. Create your first task!"}
        </div>
      ) : (
        <div className="space-y-3">
          {tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              onEdit={() => { setEditingTask(task); setShowForm(false); }}
              onDelete={() => handleDelete(task.id)}
              onStatusChange={(status) => handleStatusChange(task.id, status)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
