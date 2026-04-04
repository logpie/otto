import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";
import QRCode from "qrcode";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  const link = await prisma.link.findUnique({ where: { id } });
  if (!link) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const shortUrl = (process.env.NEXTAUTH_URL || "http://localhost:3000") + "/" + link.code;
  const buffer = await QRCode.toBuffer(shortUrl);

  return new NextResponse(new Uint8Array(buffer), {
    headers: {
      "Content-Type": "image/png",
      "Cache-Control": "public, max-age=86400",
    },
  });
}
