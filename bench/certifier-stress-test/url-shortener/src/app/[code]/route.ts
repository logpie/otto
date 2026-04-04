import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ code: string }> }
) {
  const { code } = await params;

  const link = await prisma.link.findUnique({ where: { code } });
  if (!link) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  // Track the click
  const referrer = req.headers.get("referer") || null;
  const userAgent = req.headers.get("user-agent") || null;

  await Promise.all([
    prisma.link.update({
      where: { id: link.id },
      data: {
        clicks: { increment: 1 },
        lastClickedAt: new Date(),
      },
    }),
    prisma.click.create({
      data: {
        linkId: link.id,
        referrer,
        userAgent,
      },
    }),
  ]);

  return NextResponse.redirect(link.url, 302);
}
