import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function GET(request: Request) {
  const session = await getServerSession(authOptions);
  if (!session) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { searchParams } = new URL(request.url);
  const status = searchParams.get("status");
  const sort = searchParams.get("sort") || "dueDate";
  const order = searchParams.get("order") || "asc";

  const where: Record<string, unknown> = { userId: session.user.id };
  if (status && ["TODO", "IN_PROGRESS", "DONE"].includes(status)) {
    where.status = status;
  }

  const orderBy: Record<string, string> = {};
  if (sort === "dueDate") {
    orderBy.dueDate = order === "desc" ? "desc" : "asc";
  } else if (sort === "createdAt") {
    orderBy.createdAt = order === "desc" ? "desc" : "asc";
  } else {
    orderBy.dueDate = "asc";
  }

  const tasks = await prisma.task.findMany({ where, orderBy });
  return NextResponse.json(tasks);
}

export async function POST(request: Request) {
  const session = await getServerSession(authOptions);
  if (!session) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  try {
    const { title, description, status, dueDate } = await request.json();

    if (!title?.trim()) {
      return NextResponse.json({ error: "Title is required" }, { status: 400 });
    }

    if (status && !["TODO", "IN_PROGRESS", "DONE"].includes(status)) {
      return NextResponse.json({ error: "Invalid status" }, { status: 400 });
    }

    const task = await prisma.task.create({
      data: {
        title: title.trim(),
        description: description?.trim() || "",
        status: status || "TODO",
        dueDate: dueDate ? new Date(dueDate) : null,
        userId: session.user.id,
      },
    });

    return NextResponse.json(task, { status: 201 });
  } catch {
    return NextResponse.json({ error: "Failed to create task" }, { status: 500 });
  }
}
