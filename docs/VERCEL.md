# Host the Mechus dashboard on Vercel

The repository is configured as a hybrid deployment:

- **Vercel** serves the landlord dashboard, public reports, and a small same-origin API bridge.
- **The existing Docker/Vultr service** continues to run meetings, transcription, FaceTime/iMessage integrations, media processing, and durable property data.

Those backend jobs are long-running and stateful, so they should not be moved into a short-lived Vercel Function. The generated browser configuration calls the backend directly for uploads and falls back to the same-origin bridge for ordinary API requests.

## Deploy

1. Make sure the Mechus backend is available at a public HTTPS origin, such as `https://api.mechus.example`.
2. Import this repository into Vercel. The included `vercel.json` selects the build command and output directory; use the **Other** framework preset if Vercel asks.
3. Add this environment variable for Preview and Production:

   ```text
   MECHUS_API_ORIGIN=https://api.mechus.example
   ```

4. On the backend, allow the Vercel site origin:

   ```text
   KOOV_CORS_ORIGINS=https://your-project.vercel.app
   ```

   Add the custom production domain too if one is configured. Use a comma-separated list if the backend supports both origins.

5. If generated report links should open through Vercel, set this on the backend:

   ```text
   KOOV_PUBLIC_BASE_URL=https://your-project.vercel.app
   ```

6. Deploy from the Vercel UI, or with the CLI:

   ```bash
   vercel
   vercel --prod
   ```

The Chert webhook URL must point directly to the backend, for example `https://api.mechus.example/webhooks/chert`. Do not route signed webhook requests through the Vercel bridge; the backend needs the original request body to verify the signature.

## Build and preview locally

```bash
MECHUS_API_ORIGIN=https://api.mechus.example node scripts/build-vercel.mjs
python3 -m http.server 4173 --directory public
```

Then open `http://localhost:4173/dashboard.html`. If no backend is available, build without `MECHUS_API_ORIGIN` and open `http://localhost:4173/dashboard.html?demo=1`.

Generated files are written to `public/` and intentionally ignored by Git. Vercel rebuilds them for every deployment.

## Deployment boundaries

The Vercel deployment owns:

- `/`, `/dashboard`, `/properties`, and `/meetings`
- public inspection reports at `/r/:token`
- static dashboard assets
- a lightweight bridge for JSON API calls

The container backend still owns:

- Zoom and FaceTime meeting workers
- transcription, audio, video, and AI processing
- iMessage delivery
- Chert webhooks
- durable property and inspection records
- large inspection media uploads

`GET /health` checks the Vercel frontend. `GET /ready` is forwarded to the configured backend and checks whether the complete application is ready.

## Environment variables

| Variable | Set on | Purpose |
| --- | --- | --- |
| `MECHUS_API_ORIGIN` | Vercel | Public HTTPS origin of the Mechus backend |
| `KOOV_CORS_ORIGINS` | Backend | Allows the deployed dashboard to call the backend |
| `KOOV_PUBLIC_BASE_URL` | Backend | Makes report links use the Vercel domain |

Do not copy `.env` into Vercel. Add only the required production values in Vercel project settings; `.vercelignore` excludes local secrets and heavyweight backend artifacts from CLI uploads.
