import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function GET() {
  const posts = await prisma.post.findMany({
    where: { published: true },
    include: {
      author: { select: { id: true, name: true } },
      tags: true,
      _count: { select: { likes: true, comments: true } },
    },
    orderBy: { createdAt: "desc" },
  });

  return NextResponse.json(posts);
}

export async function POST(request: Request) {
  const session = await getServerSession(authOptions);
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { title, content, published, tags } = await request.json();

  const post = await prisma.post.create({
    data: {
      title,
      content,
      published: published ?? false,
      authorId: session.user.id,
      tags: {
        connectOrCreate: (tags || []).map((tag: string) => ({
          where: { name: tag },
          create: { name: tag },
        })),
      },
    },
    include: { tags: true },
  });

  return NextResponse.json(post);
}
