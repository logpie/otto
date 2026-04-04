import { prisma } from "@/lib/prisma";
import { notFound } from "next/navigation";
import RecipeDetail from "./RecipeDetail";

export const dynamic = "force-dynamic";

export default async function RecipePage({
  params,
}: {
  params: { id: string };
}) {
  const recipe = await prisma.recipe.findUnique({
    where: { id: params.id },
    include: {
      author: { select: { id: true, name: true } },
      ratings: { select: { value: true, userId: true } },
      favorites: { select: { userId: true } },
    },
  });

  if (!recipe) notFound();

  const avgRating =
    recipe.ratings.length > 0
      ? recipe.ratings.reduce((sum, r) => sum + r.value, 0) /
        recipe.ratings.length
      : 0;

  return (
    <RecipeDetail
      recipe={{
        ...recipe,
        ingredients: JSON.parse(recipe.ingredients),
        avgRating,
        ratingCount: recipe.ratings.length,
      }}
    />
  );
}
