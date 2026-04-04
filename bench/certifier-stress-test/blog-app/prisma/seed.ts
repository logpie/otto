import { PrismaClient } from "@prisma/client";
import bcrypt from "bcryptjs";

const prisma = new PrismaClient();

async function main() {
  // Clean existing data
  await prisma.like.deleteMany();
  await prisma.comment.deleteMany();
  await prisma.post.deleteMany();
  await prisma.tag.deleteMany();
  await prisma.user.deleteMany();

  // Create users
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

  // Create tags
  const nextjsTag = await prisma.tag.create({ data: { name: "nextjs" } });
  const reactTag = await prisma.tag.create({ data: { name: "react" } });
  const typescriptTag = await prisma.tag.create({
    data: { name: "typescript" },
  });
  const webdevTag = await prisma.tag.create({ data: { name: "webdev" } });
  const tutorialTag = await prisma.tag.create({ data: { name: "tutorial" } });

  // Create posts
  const post1 = await prisma.post.create({
    data: {
      title: "Getting Started with Next.js",
      content:
        "Next.js is a powerful React framework that makes building web applications a breeze. In this post, we'll explore the basics of Next.js and how to get your first application up and running.\n\nNext.js provides features like server-side rendering, static site generation, and API routes out of the box. The App Router, introduced in Next.js 13, brings a new paradigm for building applications with React Server Components.\n\nTo get started, simply run npx create-next-app@latest and follow the prompts. You'll have a fully configured project with TypeScript, Tailwind CSS, and ESLint ready to go.",
      published: true,
      authorId: alice.id,
      tags: {
        connect: [
          { id: nextjsTag.id },
          { id: reactTag.id },
          { id: tutorialTag.id },
        ],
      },
    },
  });

  const post2 = await prisma.post.create({
    data: {
      title: "TypeScript Best Practices for 2024",
      content:
        "TypeScript has become the standard for modern web development. Here are some best practices to follow when writing TypeScript code.\n\nFirst, always enable strict mode in your tsconfig.json. This catches many common errors at compile time rather than runtime. Second, prefer interfaces over type aliases for object shapes — they provide better error messages and are more extensible.\n\nThird, use discriminated unions for state management. They make it impossible to access properties that don't exist in a given state. Finally, leverage the utility types like Partial, Required, Pick, and Omit to avoid repeating yourself.",
      published: true,
      authorId: alice.id,
      tags: { connect: [{ id: typescriptTag.id }, { id: webdevTag.id }] },
    },
  });

  const post3 = await prisma.post.create({
    data: {
      title: "Building a Blog with Prisma and SQLite",
      content:
        "Prisma is an excellent ORM for Node.js and TypeScript. Combined with SQLite, it provides a zero-configuration database solution that's perfect for prototyping and small to medium applications.\n\nPrisma's schema-first approach means you define your data model in a .prisma file, and Prisma generates a type-safe client for you. Migrations are handled automatically, and the Prisma Studio gives you a visual editor for your data.\n\nSQLite is an embedded database that stores data in a single file. It requires no server setup and is surprisingly capable — it handles concurrent reads well and is used by many production applications.",
      published: true,
      authorId: bob.id,
      tags: {
        connect: [{ id: nextjsTag.id }, { id: tutorialTag.id }],
      },
    },
  });

  const post4 = await prisma.post.create({
    data: {
      title: "React Server Components Explained",
      content:
        "React Server Components (RSC) represent a fundamental shift in how we build React applications. They allow components to render on the server, reducing the JavaScript sent to the client.\n\nWith RSC, you can directly access your database, file system, or other server-side resources from your components — no API layer needed. This simplifies your architecture and improves performance.\n\nThe key distinction is between Server Components (the default in Next.js App Router) and Client Components (marked with 'use client'). Server Components can't use hooks or browser APIs, while Client Components can't directly access server resources.",
      published: true,
      authorId: bob.id,
      tags: {
        connect: [
          { id: reactTag.id },
          { id: nextjsTag.id },
          { id: webdevTag.id },
        ],
      },
    },
  });

  await prisma.post.create({
    data: {
      title: "Advanced TypeScript Patterns",
      content:
        "This is a draft post about advanced TypeScript patterns including conditional types, template literal types, and recursive type definitions. Still working on the examples...",
      published: false,
      authorId: alice.id,
      tags: { connect: [{ id: typescriptTag.id }] },
    },
  });

  // Create 10 comments
  await prisma.comment.createMany({
    data: [
      {
        content:
          "Great introduction! This helped me get started with Next.js quickly.",
        authorId: bob.id,
        postId: post1.id,
      },
      {
        content: "Could you also cover middleware in a future post?",
        authorId: bob.id,
        postId: post1.id,
      },
      {
        content:
          "Thanks everyone! I'll write about middleware next.",
        authorId: alice.id,
        postId: post1.id,
      },
      {
        content:
          "The strict mode tip alone saved me hours of debugging. Thanks!",
        authorId: bob.id,
        postId: post2.id,
      },
      {
        content:
          "Discriminated unions are a game changer for state management.",
        authorId: bob.id,
        postId: post2.id,
      },
      {
        content: "SQLite is underrated for small projects. Great writeup!",
        authorId: alice.id,
        postId: post3.id,
      },
      {
        content: "How does Prisma handle migrations in production?",
        authorId: alice.id,
        postId: post3.id,
      },
      {
        content:
          "Server Components confused me at first, but this explanation is crystal clear.",
        authorId: alice.id,
        postId: post4.id,
      },
      {
        content:
          "The distinction between Server and Client Components is so important to understand.",
        authorId: bob.id,
        postId: post4.id,
      },
      {
        content:
          "Would love to see a comparison with traditional SSR approaches.",
        authorId: alice.id,
        postId: post4.id,
      },
    ],
  });

  // Create likes
  await prisma.like.createMany({
    data: [
      { userId: bob.id, postId: post1.id },
      { userId: alice.id, postId: post3.id },
      { userId: alice.id, postId: post4.id },
      { userId: bob.id, postId: post2.id },
      { userId: bob.id, postId: post4.id },
    ],
  });

  console.log("Seed data created successfully!");
  console.log(`Users: ${alice.name}, ${bob.name}`);
  console.log("Posts: 5 (4 published, 1 draft)");
  console.log("Comments: 10");
  console.log("Likes: 5");
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
