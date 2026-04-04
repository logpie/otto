import { PrismaClient } from "@prisma/client";
import bcrypt from "bcryptjs";

const prisma = new PrismaClient();

async function main() {
  // Clean existing data
  await prisma.task.deleteMany();
  await prisma.user.deleteMany();

  const alice = await prisma.user.create({
    data: {
      email: "alice@example.com",
      name: "Alice Johnson",
      password: await bcrypt.hash("password123", 10),
    },
  });

  const bob = await prisma.user.create({
    data: {
      email: "bob@example.com",
      name: "Bob Smith",
      password: await bcrypt.hash("password123", 10),
    },
  });

  await prisma.task.createMany({
    data: [
      {
        title: "Set up project infrastructure",
        description: "Configure CI/CD pipeline, set up staging environment, and establish coding standards.",
        status: "DONE",
        dueDate: new Date("2026-03-25"),
        userId: alice.id,
      },
      {
        title: "Design database schema",
        description: "Create ERD for the new feature module and get team approval.",
        status: "IN_PROGRESS",
        dueDate: new Date("2026-04-02"),
        userId: alice.id,
      },
      {
        title: "Write API documentation",
        description: "Document all REST endpoints with request/response examples using OpenAPI spec.",
        status: "TODO",
        dueDate: new Date("2026-04-10"),
        userId: alice.id,
      },
      {
        title: "Review pull requests",
        description: "Review and merge pending PRs from the frontend team.",
        status: "IN_PROGRESS",
        dueDate: new Date("2026-04-01"),
        userId: bob.id,
      },
      {
        title: "Prepare sprint demo",
        description: "Create slides and demo script for the end-of-sprint showcase.",
        status: "TODO",
        dueDate: new Date("2026-04-05"),
        userId: bob.id,
      },
    ],
  });

  console.log("Seed data created: 2 users, 5 tasks");
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(() => prisma.$disconnect());
