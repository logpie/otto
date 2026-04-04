"use client";

import { useSession } from "next-auth/react";
import { useRouter, useParams } from "next/navigation";
import { useState, useEffect } from "react";
import RecipeForm from "@/components/RecipeForm";

export default function EditRecipePage() {
  const { data: session, status } = useSession();
  const router = useRouter();
  const params = useParams();
  const [recipe, setRecipe] = useState<{
    title: string;
    description: string;
    ingredients: string[];
    instructions: string;
    prepTime: number;
    cookTime: number;
    servings: number;
    category: string;
    authorId: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`/api/recipes/${params.id}`)
      .then((r) => r.json())
      .then((data) => {
        setRecipe({
          ...data,
          ingredients: JSON.parse(data.ingredients),
        });
        setLoading(false);
      });
  }, [params.id]);

  if (status === "loading" || loading)
    return <div className="text-center py-12">Loading...</div>;
  if (!session) {
    router.push("/login");
    return null;
  }

  const userId = (session.user as { id?: string })?.id;
  if (recipe && recipe.authorId !== userId) {
    router.push("/");
    return null;
  }

  const handleSubmit = async (data: {
    title: string;
    description: string;
    ingredients: string[];
    instructions: string;
    prepTime: number;
    cookTime: number;
    servings: number;
    category: string;
  }) => {
    setSaving(true);
    const res = await fetch(`/api/recipes/${params.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      router.push(`/recipes/${params.id}`);
    }
    setSaving(false);
  };

  if (!recipe) return null;

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Edit Recipe</h1>
      <RecipeForm onSubmit={handleSubmit} saving={saving} initial={recipe} />
    </div>
  );
}
