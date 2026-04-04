import { PrismaClient } from "@prisma/client";
import bcrypt from "bcryptjs";

const prisma = new PrismaClient();

async function main() {
  // Clear existing data
  await prisma.favorite.deleteMany();
  await prisma.rating.deleteMany();
  await prisma.recipe.deleteMany();
  await prisma.user.deleteMany();

  // Create 2 users
  const alice = await prisma.user.create({
    data: {
      name: "Alice Johnson",
      email: "alice@example.com",
      password: await bcrypt.hash("password123", 10),
    },
  });

  const bob = await prisma.user.create({
    data: {
      name: "Bob Smith",
      email: "bob@example.com",
      password: await bcrypt.hash("password123", 10),
    },
  });

  // Create 8 recipes
  const recipes = await Promise.all([
    prisma.recipe.create({
      data: {
        title: "Classic Pancakes",
        description:
          "Fluffy buttermilk pancakes perfect for a weekend breakfast. Golden brown and delicious.",
        ingredients: JSON.stringify([
          "1.5 cups all-purpose flour",
          "3.5 tsp baking powder",
          "1 tbsp sugar",
          "1.25 cups milk",
          "1 egg",
          "3 tbsp melted butter",
          "Pinch of salt",
        ]),
        instructions:
          "1. Sift flour, baking powder, sugar, and salt together in a large bowl.\n2. Make a well in the center and pour in milk, egg, and melted butter. Mix until smooth.\n3. Heat a griddle or pan over medium-high heat. Lightly oil.\n4. Pour batter onto griddle, using about 1/4 cup per pancake.\n5. Cook until bubbles form on surface, then flip and cook until golden brown.\n6. Serve with maple syrup and fresh berries.",
        prepTime: 10,
        cookTime: 15,
        servings: 4,
        category: "breakfast",
        authorId: alice.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Avocado Toast with Poached Egg",
        description:
          "Trendy and nutritious breakfast featuring creamy avocado on crispy sourdough topped with a perfectly poached egg.",
        ingredients: JSON.stringify([
          "2 slices sourdough bread",
          "1 ripe avocado",
          "2 eggs",
          "1 tbsp white vinegar",
          "Red pepper flakes",
          "Salt and pepper",
          "Lemon juice",
        ]),
        instructions:
          "1. Toast the sourdough slices until golden and crispy.\n2. Mash the avocado with lemon juice, salt, and pepper.\n3. Bring a pot of water to a gentle simmer, add vinegar.\n4. Create a swirl in the water and carefully drop in an egg. Poach for 3-4 minutes.\n5. Spread mashed avocado on toast, top with poached egg.\n6. Season with red pepper flakes, salt, and pepper.",
        prepTime: 5,
        cookTime: 10,
        servings: 2,
        category: "breakfast",
        authorId: bob.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Caesar Salad",
        description:
          "Crisp romaine lettuce with homemade Caesar dressing, crunchy croutons, and shaved parmesan.",
        ingredients: JSON.stringify([
          "1 head romaine lettuce",
          "1/2 cup parmesan cheese, shaved",
          "1 cup croutons",
          "2 anchovy fillets",
          "1 clove garlic",
          "2 tbsp lemon juice",
          "1 tsp Dijon mustard",
          "1/3 cup olive oil",
          "1 egg yolk",
        ]),
        instructions:
          "1. Wash and chop romaine lettuce into bite-sized pieces.\n2. For the dressing: mash anchovies and garlic into a paste.\n3. Whisk in egg yolk, lemon juice, and Dijon mustard.\n4. Slowly drizzle in olive oil while whisking to emulsify.\n5. Toss lettuce with dressing until evenly coated.\n6. Top with croutons and shaved parmesan. Serve immediately.",
        prepTime: 15,
        cookTime: 0,
        servings: 4,
        category: "lunch",
        authorId: alice.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Grilled Chicken Wrap",
        description:
          "A hearty lunch wrap loaded with grilled chicken, fresh vegetables, and tangy yogurt sauce.",
        ingredients: JSON.stringify([
          "2 chicken breasts",
          "4 large flour tortillas",
          "1 cup mixed greens",
          "1 tomato, diced",
          "1/2 cucumber, sliced",
          "1/4 red onion, sliced",
          "1/2 cup Greek yogurt",
          "1 tbsp lemon juice",
          "1 tsp garlic powder",
          "Salt and pepper",
        ]),
        instructions:
          "1. Season chicken with garlic powder, salt, and pepper.\n2. Grill chicken over medium-high heat for 6-7 minutes per side until cooked through.\n3. Let chicken rest 5 minutes, then slice.\n4. Mix yogurt with lemon juice and a pinch of salt.\n5. Warm tortillas on the grill for 30 seconds.\n6. Layer yogurt sauce, greens, chicken, tomato, cucumber, and onion.\n7. Roll up tightly, tucking in the sides.",
        prepTime: 15,
        cookTime: 15,
        servings: 4,
        category: "lunch",
        authorId: bob.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Spaghetti Bolognese",
        description:
          "Rich and meaty Italian classic with slow-simmered tomato sauce over perfectly cooked pasta.",
        ingredients: JSON.stringify([
          "400g spaghetti",
          "500g ground beef",
          "1 onion, diced",
          "3 cloves garlic, minced",
          "800g canned crushed tomatoes",
          "2 tbsp tomato paste",
          "1 carrot, finely diced",
          "1 celery stalk, finely diced",
          "1/2 cup red wine",
          "Fresh basil",
          "Parmesan cheese",
          "Salt and pepper",
          "2 tbsp olive oil",
        ]),
        instructions:
          "1. Heat olive oil in a large pot. Sauté onion, carrot, and celery until soft.\n2. Add garlic and cook for 1 minute.\n3. Add ground beef, breaking it up, and cook until browned.\n4. Pour in red wine and let it reduce by half.\n5. Add crushed tomatoes and tomato paste. Season with salt and pepper.\n6. Simmer on low for at least 30 minutes, stirring occasionally.\n7. Cook spaghetti according to package directions.\n8. Serve sauce over pasta, topped with fresh basil and parmesan.",
        prepTime: 15,
        cookTime: 45,
        servings: 4,
        category: "dinner",
        authorId: alice.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Teriyaki Salmon",
        description:
          "Glazed salmon fillets with a sweet and savory homemade teriyaki sauce, served with steamed rice.",
        ingredients: JSON.stringify([
          "4 salmon fillets",
          "1/4 cup soy sauce",
          "2 tbsp mirin",
          "2 tbsp sake",
          "1 tbsp sugar",
          "1 tsp ginger, grated",
          "2 cups jasmine rice",
          "Sesame seeds",
          "Green onions",
        ]),
        instructions:
          "1. Mix soy sauce, mirin, sake, sugar, and ginger for the teriyaki sauce.\n2. Marinate salmon in half the sauce for 15 minutes.\n3. Cook rice according to package directions.\n4. Heat a skillet over medium-high heat. Sear salmon skin-side up for 3 minutes.\n5. Flip and cook for 3 more minutes.\n6. Pour remaining sauce over salmon and let it glaze for 1 minute.\n7. Serve over rice, garnished with sesame seeds and sliced green onions.",
        prepTime: 20,
        cookTime: 10,
        servings: 4,
        category: "dinner",
        authorId: bob.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Chocolate Lava Cake",
        description:
          "Decadent individual chocolate cakes with a molten center. An impressive dessert that is easier than it looks.",
        ingredients: JSON.stringify([
          "200g dark chocolate",
          "100g butter",
          "2 eggs",
          "2 egg yolks",
          "1/4 cup sugar",
          "2 tbsp flour",
          "Butter and cocoa for ramekins",
          "Vanilla ice cream for serving",
        ]),
        instructions:
          "1. Preheat oven to 425°F (220°C). Butter and dust 4 ramekins with cocoa powder.\n2. Melt chocolate and butter together in a double boiler.\n3. Whisk eggs, egg yolks, and sugar until thick and pale.\n4. Fold chocolate mixture into egg mixture.\n5. Gently fold in flour until just combined.\n6. Divide batter among ramekins.\n7. Bake for 12-14 minutes until edges are set but center is soft.\n8. Let cool 1 minute, then invert onto plates. Serve with vanilla ice cream.",
        prepTime: 15,
        cookTime: 14,
        servings: 4,
        category: "dessert",
        authorId: alice.id,
      },
    }),
    prisma.recipe.create({
      data: {
        title: "Tiramisu",
        description:
          "Classic Italian no-bake dessert with layers of coffee-soaked ladyfingers and mascarpone cream.",
        ingredients: JSON.stringify([
          "6 egg yolks",
          "3/4 cup sugar",
          "500g mascarpone cheese",
          "2 cups heavy cream",
          "2 cups strong espresso, cooled",
          "3 tbsp coffee liqueur",
          "24 ladyfinger biscuits",
          "Cocoa powder",
          "Dark chocolate shavings",
        ]),
        instructions:
          "1. Whisk egg yolks and sugar until thick and pale.\n2. Add mascarpone and mix until smooth.\n3. Whip heavy cream to stiff peaks, fold into mascarpone mixture.\n4. Combine cooled espresso with coffee liqueur.\n5. Quickly dip each ladyfinger in the coffee mixture.\n6. Layer dipped ladyfingers in a 9x13 dish.\n7. Spread half the mascarpone cream over the ladyfingers.\n8. Repeat layers.\n9. Dust generously with cocoa powder and top with chocolate shavings.\n10. Refrigerate at least 4 hours, preferably overnight.",
        prepTime: 30,
        cookTime: 0,
        servings: 8,
        category: "dessert",
        authorId: bob.id,
      },
    }),
  ]);

  // Add some ratings
  await prisma.rating.createMany({
    data: [
      { value: 5, userId: bob.id, recipeId: recipes[0].id },
      { value: 4, userId: bob.id, recipeId: recipes[2].id },
      { value: 5, userId: bob.id, recipeId: recipes[4].id },
      { value: 4, userId: bob.id, recipeId: recipes[6].id },
      { value: 4, userId: alice.id, recipeId: recipes[1].id },
      { value: 5, userId: alice.id, recipeId: recipes[3].id },
      { value: 5, userId: alice.id, recipeId: recipes[5].id },
      { value: 5, userId: alice.id, recipeId: recipes[7].id },
    ],
  });

  // Add some favorites
  await prisma.favorite.createMany({
    data: [
      { userId: alice.id, recipeId: recipes[1].id },
      { userId: alice.id, recipeId: recipes[5].id },
      { userId: bob.id, recipeId: recipes[0].id },
      { userId: bob.id, recipeId: recipes[4].id },
      { userId: bob.id, recipeId: recipes[6].id },
    ],
  });

  console.log("Seeded 2 users, 8 recipes, 8 ratings, 5 favorites");
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(() => prisma.$disconnect());
