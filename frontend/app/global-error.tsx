"use client";

// Shown when an error escapes even the root layout, the last line of
// defence. Reports the error to Sentry and renders Next.js's default error
// page (statusCode 0: the App Router doesn't expose a status here).
import * as Sentry from "@sentry/nextjs";
import NextError from "next/error";
import { useEffect } from "react";

export default function GlobalError({ error }: { error: Error & { digest?: string } }) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body>
        <NextError statusCode={0} />
      </body>
    </html>
  );
}
