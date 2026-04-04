import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function POST(
  request: Request,
  { params }: { params: { id: string } }
) {
  const session = await getServerSession(authOptions);
  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const userId = (session.user as { id: string }).id;
  const { value } = await request.json();

  if (!value || value < 1 || value > 5) {
    return NextResponse.json({ error: "Rating must be 1-5" }, { status: 400 });
  }

  const rating = await prisma.rating.upsert({
    where: { userId_recipeId: { userId, recipeId: params.id } },
    update: { value },
    create: { value, userId, recipeId: params.id },
  });

  return NextResponse.json(rating);
}
