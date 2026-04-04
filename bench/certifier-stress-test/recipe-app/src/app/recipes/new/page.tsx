"use client";

import { useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import RecipeForm from "@/components/RecipeForm";

export default function NewRecipePage() {
  const { data: session, status } = useSession();
  const router = useRouter();
  const [saving, setSaving] = useState(false);

  if (status === "loading") return <div className="text-center py-12">Loading...</div>;
  if (!session) {
    router.push("/login");
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
    const res = await fetch("/api/recipes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      const recipe = await res.json();
      router.push(`/recipes/${recipe.id}`);
    }
    setSaving(false);
  };

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Create New Recipe</h1>
      <RecipeForm onSubmit={handleSubmit} saving={saving} />
    </div>
  );
}
