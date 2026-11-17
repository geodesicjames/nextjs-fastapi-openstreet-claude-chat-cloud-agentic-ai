// Inputs shared by the approve form and the grant edit form.

const inputClass =
  "w-full rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700";

export function GrantFields({
  idPrefix,
  note = "",
  dailyTokenLimit = null,
  expiresOn = "",
}: {
  idPrefix: string;
  note?: string;
  dailyTokenLimit?: number | null;
  expiresOn?: string;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <label className="flex flex-col gap-1 text-sm">
        Daily token limit
        <input
          id={`${idPrefix}-limit`}
          name="daily_token_limit"
          type="number"
          min={1}
          step={1}
          defaultValue={dailyTokenLimit ?? ""}
          placeholder="Default"
          className={inputClass}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Expires (end of day, UTC)
        <input
          id={`${idPrefix}-expires`}
          name="expires_on"
          type="date"
          defaultValue={expiresOn}
          className={inputClass}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Note
        <input
          id={`${idPrefix}-note`}
          name="note"
          maxLength={500}
          defaultValue={note}
          placeholder="e.g. Recruiter at Acme"
          className={inputClass}
        />
      </label>
    </div>
  );
}

export function ActionError({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="text-sm text-red-600">
      {error}
    </p>
  ) : null;
}

export const primaryButton =
  "rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900";
export const secondaryButton =
  "rounded-md border border-neutral-300 px-3 py-1.5 text-sm disabled:opacity-60 dark:border-neutral-700";
