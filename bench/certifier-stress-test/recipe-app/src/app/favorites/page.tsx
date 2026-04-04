import { prisma } from "@/lib/prisma";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { redirect } from "next/navigation";
import RecipeCard from "@/components/RecipeCard";

export const dynamic = "force-dynamic";

export default async function FavoritesPage() {
  const session = await getServerSession(authOptions);
  if (!session) redirect("/login");

  const userId = (session.user as { id: string }).id;

  const favorites = await prisma.favorite.findMany({
    where: { userId },
    include: {
      recipe: {
        include: {
          author: { select: { name: true } },
          ratings: { select: { value: true } },
        },
      },
    },
  });

  return (
    <div>
      <h1 className="text-3xl font-bold text-gray-900 mb-2">
        My Favorites
      </h1>
      <p className="text-gray-600 mb-8">Recipes you&apos;ve saved</p>

      {favorites.length === 0 ? (
        <p className="text-gray-500 text-center py-12">
          You haven&apos;t favorited any recipes yet.
        </p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {favorites.map(({ recipe }) => {
            const avg =
              recipe.ratings.length > 0
                ? recipe.ratings.reduce((sum, r) => sum + r.value, 0) /
                  recipe.ratings.length
                : 0;
            return (
              <RecipeCard
                key={recipe.id}
                id={recipe.id}
                title={recipe.title}
                description={recipe.description}
                category={recipe.category}
                prepTime={recipe.prepTime}
                cookTime={recipe.cookTime}
                servings={recipe.servings}
                authorName={recipe.author.name}
                avgRating={avg}
                ratingCount={recipe.ratings.length}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
