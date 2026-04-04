import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function POST(
  request: Request,
  { params }: { params: { id: string } }
) {
  const session = await getServerSession(authOptions);
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { content } = await request.json();

  if (!content) {
    return NextResponse.json({ error: "Content required" }, { status: 400 });
  }

  const comment = await prisma.comment.create({
    data: {
      content,
      authorId: session.user.id,
      postId: params.id,
    },
    include: {
      author: { select: { id: true, name: true } },
    },
  });

  return NextResponse.json(comment);
}
