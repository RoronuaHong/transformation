import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { locales } from "@/lib/locales";

/** Redirect /zh/zh/… → /zh/… and legacy /{locale}/try → /{locale}. */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  for (const loc of locales) {
    const dup = `/${loc}/${loc}`;
    if (pathname === dup || pathname.startsWith(`${dup}/`)) {
      const url = request.nextUrl.clone();
      url.pathname = pathname.replace(dup, `/${loc}`);
      return NextResponse.redirect(url);
    }
    if (pathname === `/${loc}/try`) {
      const url = request.nextUrl.clone();
      url.pathname = `/${loc}`;
      return NextResponse.redirect(url);
    }
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|.*\\..*).*)"],
};
