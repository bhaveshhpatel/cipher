
import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json(
    { message: "NextAuth is not configured in this application. Cipher uses custom JWT auth via the FastAPI backend." },
    { status: 501 }
  );
}

export async function POST() {
  return NextResponse.json(
    { message: "NextAuth is not configured in this application. Cipher uses custom JWT auth via the FastAPI backend." },
    { status: 501 }
  );
}
