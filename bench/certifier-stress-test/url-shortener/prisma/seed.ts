import { PrismaClient } from "@prisma/client";
import { hashSync } from "bcryptjs";

const prisma = new PrismaClient();

async function main() {
  await prisma.click.deleteMany();
  await prisma.link.deleteMany();
  await prisma.user.deleteMany();

  const user = await prisma.user.create({
    data: {
      email: "demo@example.com",
      name: "Demo User",
      password: hashSync("password123", 10),
    },
  });

  const links = [
    {
      code: "github",
      url: "https://github.com/vercel/next.js",
      clicks: 142,
      lastClickedAt: new Date("2026-03-30T14:30:00Z"),
    },
    {
      code: "yt-vid",
      url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      clicks: 89,
      lastClickedAt: new Date("2026-03-29T09:15:00Z"),
    },
    {
      code: "docs",
      url: "https://nextjs.org/docs/getting-started/installation",
      clicks: 37,
      lastClickedAt: new Date("2026-03-28T18:45:00Z"),
    },
    {
      code: "tw-css",
      url: "https://tailwindcss.com/docs/installation",
      clicks: 15,
      lastClickedAt: null,
    },
    {
      code: "prisma",
      url: "https://www.prisma.io/docs/getting-started",
      clicks: 4,
      lastClickedAt: new Date("2026-03-25T12:00:00Z"),
    },
  ];

  for (const link of links) {
    await prisma.link.create({
      data: {
        code: link.code,
        url: link.url,
        userId: user.id,
        clicks: link.clicks,
        lastClickedAt: link.lastClickedAt,
      },
    });
  }

  console.log("Seed complete: 1 user (demo@example.com / password123), 5 links created.");
}

main()
  .then(() => prisma.$disconnect())
  .catch((e) => {
    console.error(e);
    prisma.$disconnect();
    process.exit(1);
  });
