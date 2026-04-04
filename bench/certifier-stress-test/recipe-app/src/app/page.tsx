import { prisma } from "@/lib/prisma";
import RecipeCard from "@/components/RecipeCard";
import Link from "next/link";

const categories = ["breakfast", "lunch", "dinner", "dessert"];

export const dynamic = "force-dynamic";

export default async function Home({
  searchParams,
}: {
  searchParams: { category?: string };
}) {
  const activeCategory = searchParams.category;

  const recipes = await prisma.recipe.findMany({
    where: activeCategory ? { category: activeCategory } : undefined,
    include: {
      author: { select: { name: true } },
      ratings: { select: { value: true } },
    },
    orderBy: { createdAt: "desc" },
  });

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 mb-2">
          Discover Recipes
        </h1>
        <p className="text-gray-600">
          Browse delicious recipes shared by our community
        </p>
      </div>

      <div className="flex gap-2 mb-6">
        <Link
          href="/"
          className={`px-4 py-2 rounded-full text-sm font-medium ${
            !activeCategory
              ? "bg-orange-600 text-white"
              : "bg-white text-gray-600 border hover:bg-gray-50"
          }`}
        >
          All
        </Link>
        {categories.map((cat) => (
          <Link
            key={cat}
            href={`/?category=${cat}`}
            className={`px-4 py-2 rounded-full text-sm font-medium capitalize ${
              activeCategory === cat
                ? "bg-orange-600 text-white"
                : "bg-white text-gray-600 border hover:bg-gray-50"
            }`}
          >
            {cat}
          </Link>
        ))}
      </div>

      {recipes.length === 0 ? (
        <p className="text-gray-500 text-center py-12">
          No recipes found. Be the first to share one!
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
