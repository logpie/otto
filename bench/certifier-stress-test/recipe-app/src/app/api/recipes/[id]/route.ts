import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function GET(
  _request: Request,
  { params }: { params: { id: string } }
) {
  const recipe = await prisma.recipe.findUnique({
    where: { id: params.id },
    include: {
      author: { select: { id: true, name: true } },
      ratings: { select: { value: true, userId: true } },
      favorites: { select: { userId: true } },
    },
  });

  if (!recipe) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(recipe);
}

export async function PUT(
  request: Request,
  { params }: { params: { id: string } }
) {
  const session = await getServerSession(authOptions);
  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const userId = (session.user as { id: string }).id;
  const recipe = await prisma.recipe.findUnique({ where: { id: params.id } });

  if (!recipe || recipe.authorId !== userId) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const data = await request.json();
  const updated = await prisma.recipe.update({
    where: { id: params.id },
    data: {
      title: data.title,
      description: data.description,
      ingredients: JSON.stringify(data.ingredients),
      instructions: data.instructions,
      prepTime: data.prepTime,
      cookTime: data.cookTime,
      servings: data.servings,
      category: data.category,
    },
  });

  return NextResponse.json(updated);
}

export async function DELETE(
  _request: Request,
  { params }: { params: { id: string } }
) {
  const session = await getServerSession(authOptions);
  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const userId = (session.user as { id: string }).id;
  const recipe = await prisma.recipe.findUnique({ where: { id: params.id } });

  if (!recipe || recipe.authorId !== userId) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  await prisma.recipe.delete({ where: { id: params.id } });
  return NextResponse.json({ success: true });
}
