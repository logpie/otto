import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

async function getAuthedTask(taskId: string) {
  const session = await getServerSession(authOptions);
  if (!session) return { error: "Unauthorized", status: 401 } as const;

  const task = await prisma.task.findUnique({ where: { id: taskId } });
  if (!task) return { error: "Task not found", status: 404 } as const;
  if (task.userId !== session.user.id) return { error: "Forbidden", status: 403 } as const;

  return { task, session } as const;
}

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getAuthedTask(id);
  if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });
  return NextResponse.json(result.task);
}

export async function PUT(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getAuthedTask(id);
  if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });

  try {
    const { title, description, status, dueDate } = await request.json();

    if (title !== undefined && !title?.trim()) {
      return NextResponse.json({ error: "Title cannot be empty" }, { status: 400 });
    }

    if (status && !["TODO", "IN_PROGRESS", "DONE"].includes(status)) {
      return NextResponse.json({ error: "Invalid status" }, { status: 400 });
    }

    const data: Record<string, unknown> = {};
    if (title !== undefined) data.title = title.trim();
    if (description !== undefined) data.description = description.trim();
    if (status !== undefined) data.status = status;
    if (dueDate !== undefined) data.dueDate = dueDate ? new Date(dueDate) : null;

    const updated = await prisma.task.update({ where: { id }, data });
    return NextResponse.json(updated);
  } catch {
    return NextResponse.json({ error: "Failed to update task" }, { status: 500 });
  }
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getAuthedTask(id);
  if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });

  await prisma.task.delete({ where: { id } });
  return NextResponse.json({ success: true });
}
