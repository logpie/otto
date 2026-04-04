import { prisma } from "@/lib/prisma";
import RecipeCard from "@/components/RecipeCard";

export const dynamic = "force-dynamic";

export default async function SearchPage({
  searchParams,
}: {
  searchParams: { q?: string };
}) {
  const query = searchParams.q || "";

  const recipes = query
    ? await prisma.recipe.findMany({
        where: {
          OR: [
            { title: { contains: query } },
            { description: { contains: query } },
            { ingredients: { contains: query } },
          ],
        },
        include: {
          author: { select: { name: true } },
          ratings: { select: { value: true } },
        },
        orderBy: { createdAt: "desc" },
      })
    : [];

  return (
    <div>
      <h1 className="text-3xl font-bold text-gray-900 mb-2">Search Results</h1>
      {query && (
        <p className="text-gray-600 mb-8">
          {recipes.length} result{recipes.length !== 1 ? "s" : ""} for &quot;{query}&quot;
        </p>
      )}

      {!query ? (
        <p className="text-gray-500 text-center py-12">
          Enter a search term to find recipes by title or ingredient.
        </p>
      ) : recipes.length === 0 ? (
        <p className="text-gray-500 text-center py-12">
          No recipes found matching &quot;{query}&quot;.
        </p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {recipes.map((recipe) => {
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
