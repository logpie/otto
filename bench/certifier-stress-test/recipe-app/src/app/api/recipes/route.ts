import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function GET() {
  const recipes = await prisma.recipe.findMany({
    include: {
      author: { select: { name: true } },
      ratings: { select: { value: true } },
    },
    orderBy: { createdAt: "desc" },
  });
  return NextResponse.json(recipes);
}

export async function POST(request: Request) {
  const session = await getServerSession(authOptions);
  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const userId = (session.user as { id: string }).id;
  const data = await request.json();

  const recipe = await prisma.recipe.create({
    data: {
      title: data.title,
      description: data.description,
      ingredients: JSON.stringify(data.ingredients),
      instructions: data.instructions,
      prepTime: data.prepTime,
      cookTime: data.cookTime,
      servings: data.servings,
      category: data.category,
      authorId: userId,
    },
  });

  return NextResponse.json(recipe);
}
