import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useSearchParameterOptions } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { ErrorState, LoadingState } from "../../components/States";
import { useWorkspaceDraft } from "../../hooks/useWorkspaceDraft";
import { COUNTRIES } from "./countries";
import { CountryTable } from "./CountryTable";
import { hasNumberErrors, normalize, parametersForSave, placeLabel, type Draft } from "./draft";
import { Focus } from "./Focus";
import { Level } from "./Level";
import { OpportunityThesis } from "./OpportunityThesis";
import { Pay } from "./Pay";
import { Places } from "./Places";
import { TargetTitles } from "./TargetTitles";
import { Working } from "./Working";
import { useSearchParameterSave, type ConflictRecovery } from "./useSearchParameterSave";

const SECTIONS = [
  {
    key: "roles",
    title: "Role preferences",
    scope:
      "Clears basic level limits, excluded titles, employment limits, employers to exclude, role boundaries, and excluded conditions. Target titles remain and steer discovery. Advanced preferences stay in place.",
  },
  {
    key: "places",
    title: "Places and residence",
    scope:
      "Removes all named places, their authorization and residence answers, their local arrangement overrides, and associated pay bands. Country vocabulary stays in place.",
  },
  {
    key: "working",
    title: "Working arrangements",
    scope:
      "Clears general arrangement, attendance, travel, relocation, and time-zone limits. Local arrangement overrides on named places stay in place.",
  },
  {
    key: "pay",
    title: "Optional pay preferences",
    scope: "Removes all pay bands. No minimum, target, or stretch pay is stated.",
  },
] as const;
type WalkthroughSection = (typeof SECTIONS)[number]["key"];

function sectionError(draft: Draft, section: WalkthroughSection): string | null {
  switch (section) {
    case "roles":
      return (draft.seniority.target_titles ?? []).some((item) => item.title.trim())
        ? null
        : "Add at least one target title to finish this walkthrough and scan employer boards. Individual posting links remain available without it.";
    case "places":
      if (!draft.locations.length) return "Add a place, or choose No restriction.";
      return draft.locations.some(
        (place) => !place.key.trim() || !COUNTRIES.includes(place.country) || !place.eligibility,
      )
        ? "Choose a country and your standing for each place. Residence does not grant authorization."
        : null;
    case "working":
      if (hasNumberErrors(draft))
        return "Correct the working-arrangement numbers before continuing.";
      return draft.working_time_zone ||
        Object.values(draft.arrangement).some((value) => value !== null && value !== undefined)
        ? null
        : "Supply a working preference, or choose No restriction.";
    case "pay":
      if (!draft.compensation.length) return "Add a pay band, or choose No restriction.";
      return draft.compensation.some(
        (band) =>
          !draft.locations.some((place) => place.key === band.location) ||
          !band.currency.trim() ||
          ![band.minimum, band.target, band.stretch].some(
            (value) => value !== null && value !== undefined,
          ) ||
          [band.minimum, band.target, band.stretch].some(
            (value) =>
              value !== null &&
              value !== undefined &&
              (!Number.isFinite(Number(value)) || Number(value) < 0),
          ),
      )
        ? "Each pay band needs a place, currency, and at least one non-negative amount."
        : null;
  }
}

function withoutBasicRestrictions(draft: Draft, section: WalkthroughSection): Draft {
  const empty = normalize(undefined);
  switch (section) {
    case "roles":
      return {
        ...draft,
        seniority: {
          ...draft.seniority,
          minimum: "",
          maximum: "",
          excluded_titles: [],
          title_matching: "guided",
        },
        employment: { ...draft.employment, types: [], excluded_types: [] },
        employers: { ...draft.employers, excluded_employers: [] },
        focus: { ...draft.focus, boundaries: [] },
        excluded_conditions: [],
      };
    case "places":
      return { ...draft, locations: [], compensation: [] };
    case "working":
      return {
        ...draft,
        arrangement: empty.arrangement,
        working_time_zone: null,
        working_day_shift_hours: 0,
      };
    case "pay":
      return { ...draft, compensation: [] };
  }
}

function SearchPreferenceReview({
  draft,
  heading = "Review your search preferences",
}: {
  draft: Draft;
  heading?: string;
}) {
  const headingId = useId();
  const list = (items: string[] | undefined) => (items?.length ? items.join(", ") : "Not stated");
  const answer = (value: boolean | null | undefined) =>
    value === null || value === undefined ? "Not stated" : value ? "yes" : "no";
  const arrangement = (value: Draft["locations"][number]["arrangement"]) =>
    value
      ? Object.entries(value)
          .filter(([, entry]) => entry !== null && entry !== undefined)
          .map(([key, entry]) => `${key.replaceAll("_", " ")}: ${entry}`)
          .join("; ") || "Not stated"
      : "Same as general answers";
  return (
    <section className="panel" aria-labelledby={headingId}>
      <header>
        <h2 id={headingId}>{heading}</h2>
      </header>
      <div className="panel-body">
        <p>
          Only this confirmed save changes the preferences in force. Blank answers remain unstated.
          Preferences and refusals apply when a posting states the relevant facts.
        </p>
        <p>Work description (soft preference): {draft.opportunity_thesis || "Not stated"}.</p>
        <h3>Roles and exclusions</h3>
        <p>Target titles: {list(draft.seniority.target_titles.map((item) => item.title))}.</p>
        <p>
          {draft.seniority.title_matching === "strict"
            ? "Only matching posting titles are allowed."
            : "Target titles steer discovery; other titles are not excluded."}
        </p>
        <p>
          Level limits: {draft.seniority.minimum || "Not stated"} to{" "}
          {draft.seniority.maximum || "Not stated"}.
        </p>
        <p>Titles to exclude: {list(draft.seniority.excluded_titles)}.</p>
        <p>
          Employment types: {list(draft.employment.types)}; excluded types:{" "}
          {list(draft.employment.excluded_types)}.
        </p>
        <p>Employers to exclude: {list(draft.employers.excluded_employers)}.</p>
        <p>Role boundaries (soft preferences): {list(draft.focus.boundaries)}.</p>
        <p>
          Excluded conditions:{" "}
          {list(draft.excluded_conditions.map(([condition, reason]) => `${condition}: ${reason}`))}.
        </p>
        <h3>Places and residence</h3>
        {draft.locations.length ? (
          <ul>
            {draft.locations.map((place) => (
              <li key={place.key}>
                {placeLabel(place)}: {place.eligibility || "Standing not stated"}; residence:{" "}
                {answer(place.resident)}; basis: {place.basis || "Not stated"}.
                {place.arrangement && (
                  <p>Local arrangement override: {arrangement(place.arrangement)}.</p>
                )}
                {(place.aliases ?? []).length > 0 && <p>Place aliases: {list(place.aliases)}.</p>}
              </li>
            ))}
          </ul>
        ) : (
          <p>No named places; no geography or authorization facts added.</p>
        )}
        <p>
          Authorization and residence are separate. A remote job may still require local residence
          or payroll eligibility.
        </p>
        <h3>Working arrangements</h3>
        <p>
          Remote: {draft.arrangement.remote || "Not stated"}; hybrid:{" "}
          {draft.arrangement.hybrid || "Not stated"}; on-site:{" "}
          {draft.arrangement.on_site || "Not stated"}.
        </p>
        <p>
          Office attendance:{" "}
          {draft.arrangement.maximum_office_days === null
            ? "No limit stated"
            : `at most ${draft.arrangement.maximum_office_days} days per week`}
          .
        </p>
        <p>
          Travel:{" "}
          {draft.arrangement.maximum_travel_percent === null
            ? "No limit stated"
            : `at most ${draft.arrangement.maximum_travel_percent}%`}
          ; office travel must be paid: {answer(draft.arrangement.onsite_requires_paid_travel)};
          relocate: {answer(draft.arrangement.relocation)}.
        </p>
        <p>
          {draft.working_time_zone
            ? `${draft.working_time_zone}; shift allowance: ${draft.working_day_shift_hours} hours. Zero allows no required time-zone shift.`
            : "Time zone not stated; no time-zone restriction."}
        </p>
        <h3>Pay</h3>
        {draft.compensation.length ? (
          <ul>
            {draft.compensation.map((band, index) => (
              <li key={index}>
                {placeLabel(draft.locations.find((place) => place.key === band.location)!)}:{" "}
                {band.currency} {band.basis}; minimum {band.minimum ?? "Not stated"}; target{" "}
                {band.target ?? "Not stated"}; stretch {band.stretch ?? "Not stated"}.
              </li>
            ))}
          </ul>
        ) : (
          <p>No pay restriction stated.</p>
        )}
        <p>
          Pay is compared only in the same currency and on the same basis. Currency is never
          converted; unmatched currency remains unknown.
        </p>
        <h3>Advanced preferences retained</h3>
        <p>Payroll arrangements: {list(draft.employment.payroll_arrangements)}.</p>
        <p>
          Industries: {list(draft.employers.industries)}; excluded industries:{" "}
          {list(draft.employers.excluded_industries)}; employer sizes: {list(draft.employers.sizes)}
          .
        </p>
        <p>
          Employer types:{" "}
          {list(
            draft.employers.archetypes?.map(
              (item) =>
                `${item.name}: ${item.because}; associated employers: ${list(item.employer_names)}`,
            ),
          )}
          .
        </p>
        <p>
          Capabilities:{" "}
          {list(draft.focus.capabilities?.map((item) => `${item.name} (${list(item.terms)})`))}.
        </p>
        <p>
          Domains: {list(draft.focus.domains?.map((item) => `${item.name} (${list(item.terms)})`))}.
        </p>
        <p>
          Specialist role nouns: {list(draft.seniority.specialist_ic_role_nouns)}; strong domain
          tokens: {list(draft.seniority.strong_domain_tokens)}.
        </p>
        <p>
          Scoring policy: {draft.score_weight_set || "the default policy"}, chosen under{" "}
          <Link to="/find/scoring">How jobs are scored</Link>.
        </p>
        <p>
          Country aliases:{" "}
          {list(draft.countries.map((country) => `${country.country}: ${list(country.spellings)}`))}
          .
        </p>
      </div>
    </section>
  );
}

function SearchParameterConflictReview({
  draft,
  recovery,
  onRead,
  onUseDraft,
  onUseLatest,
}: {
  draft: Draft;
  recovery: ConflictRecovery | null;
  onRead: () => void;
  onUseDraft: () => void;
  onUseLatest: () => void;
}) {
  const headingId = useId();
  const latest = recovery?.latest;
  return (
    <section className="panel" aria-labelledby={headingId}>
      <header>
        <h2 id={headingId}>Review conflicting preferences</h2>
      </header>
      <div className="panel-body">
        <p>
          The saved preferences changed while this draft was open. Your draft and section answers
          are preserved. Read the latest version before choosing which preferences to keep.
        </p>
        <button
          className="button button--quiet"
          type="button"
          disabled={recovery?.reading}
          onClick={onRead}
        >
          {recovery?.error ? "Retry reading latest preferences" : "Read latest saved preferences"}
        </button>
        {recovery?.reading && <p role="status">Reading the latest saved preferences.</p>}
        {recovery?.error && (
          <p role="alert">
            Could not read the latest preferences: {recovery.error} Your draft remains unchanged.
            Retry when ready.
          </p>
        )}
        {latest && (
          <>
            <p>
              Saved version {latest.version} is in force. Keeping the preserved draft will replace
              all of these saved preferences when you explicitly save. The versions are not merged.
              Reading and choosing a draft do not save it.
            </p>
            <SearchPreferenceReview
              heading={`Saved preferences: version ${latest.version}`}
              draft={normalize(latest.parameters)}
            />
            <SearchPreferenceReview heading="Preserved draft" draft={draft} />
            <div className="button-row">
              <button
                className="button button--primary"
                type="button"
                disabled={recovery?.reading}
                onClick={onUseDraft}
              >
                Use preserved draft against version {latest.version}
              </button>
              <ConfirmedActionButton
                className="button button--quiet"
                disabled={recovery?.reading}
                onConfirm={onUseLatest}
                options={{
                  title: "Discard the preserved draft?",
                  description: `Use saved version ${latest.version}. Your unfinished draft and its section answers will be discarded. No new version is saved.`,
                  confirmLabel: "Use latest saved preferences",
                  danger: true,
                }}
              >
                Use latest saved preferences
              </ConfirmedActionButton>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

export function SearchParameterSetup({
  workspaceId,
  mode = "advanced",
  onSkip,
  onSaved,
}: {
  workspaceId: string;
  mode?: "advanced" | "walkthrough";
  onSkip?: () => void;
  onSaved?: () => void;
}) {
  const [view, setView] = useState<"basic" | "advanced" | "paused" | null>(null);
  const options = useSearchParameterOptions();
  const {
    stored,
    save,
    conflicted,
    recovery: activeRecovery,
    readLatest,
    submit,
    reset,
  } = useSearchParameterSave(workspaceId);
  const { edits, setEdits, expectedVersion, storageError } = useWorkspaceDraft<Draft>(
    workspaceId,
    "search",
    stored.data?.version ?? 0,
  );

  const serverVersion = stored.data?.version ?? 0;
  const saved = useMemo(() => normalize(stored.data?.parameters), [stored.data?.parameters]);
  // This form never edits the scoring policy, so a save keeps the stored version's choice.
  const draft = edits ? { ...edits, score_weight_set: saved.score_weight_set } : saved;
  const dirty = edits !== null && JSON.stringify(draft) !== JSON.stringify(saved);

  const progress = draft.walkthrough ?? { step: 0, paused: false, answers: {} };
  const currentView =
    view ?? (mode === "walkthrough" ? (progress.paused ? "paused" : "basic") : "advanced");
  const step = Math.min(Math.max(progress.step, 0), SECTIONS.length);
  const walkthroughHeading = useRef<HTMLHeadingElement>(null);
  const ready =
    !stored.isLoading &&
    !options.isLoading &&
    Boolean(options.data) &&
    !(stored.isError && !stored.data) &&
    !options.isError;
  useEffect(() => {
    if (ready && currentView === "basic") walkthroughHeading.current?.focus();
  }, [ready, currentView, step, workspaceId]);

  if (stored.isLoading || options.isLoading)
    return <LoadingState label="Reading your search parameters" />;
  if (stored.isError && !stored.data) return <ErrorState error={stored.error} />;
  if (options.isError) return <ErrorState error={options.error} />;
  if (!options.data) return <LoadingState label="Reading your search parameters" />;

  const section = SECTIONS[step];
  const recoveryPanel = conflicted ? (
    <SearchParameterConflictReview
      draft={draft}
      recovery={activeRecovery}
      onRead={() => {
        void readLatest();
      }}
      onUseDraft={() => {
        if (!activeRecovery?.latest || activeRecovery.reading) return;
        setEdits(draft, activeRecovery.latest.version);
        reset();
      }}
      onUseLatest={() => {
        if (!activeRecovery?.latest || activeRecovery.reading) return;
        setEdits(null);
        reset();
      }}
    />
  ) : null;
  const invalidPlaces = draft.locations.some((place) => !place.eligibility);
  const invalidPay = draft.compensation.length > 0 && sectionError(draft, "pay") !== null;
  const patch = (changes: Partial<Draft>) => {
    const answers = { ...progress.answers };
    if (
      changes.seniority ||
      changes.employment ||
      changes.employers ||
      changes.focus ||
      changes.excluded_conditions
    )
      delete answers.roles;
    if (changes.locations) {
      delete answers.places;
      delete answers.pay;
    }
    if (
      changes.arrangement ||
      changes.working_time_zone !== undefined ||
      changes.working_day_shift_hours !== undefined
    )
      delete answers.working;
    if (changes.compensation) delete answers.pay;
    setEdits({
      ...draft,
      ...changes,
      ...(draft.walkthrough || currentView === "basic"
        ? { walkthrough: { ...progress, answers } }
        : {}),
    });
  };
  const move = (next: number) =>
    setEdits({ ...draft, walkthrough: { ...progress, step: next, paused: false } });
  const begin = () => {
    setEdits({ ...draft, walkthrough: { ...progress, paused: false } });
    setView("basic");
  };
  const saveDraft = () => {
    submit(parametersForSave(draft), expectedVersion, () => {
      setEdits(null);
      if (currentView === "basic") setView("paused");
      onSaved?.();
    });
  };

  if (currentView === "paused")
    return (
      <section className="panel">
        <header>
          <h2>Search setup</h2>
        </header>
        <div className="panel-body">
          {save.isSuccess && !dirty ? (
            <p role="status">Search preferences saved.</p>
          ) : (
            <p>
              Walkthrough paused. Unfinished answers stay in this browser tab. Saved preferences
              remain in force.
            </p>
          )}
          {storageError && (
            <p role="alert">
              Browser draft storage is unavailable. Keep this page open to preserve your unfinished
              answers.
            </p>
          )}
          <div className="button-row">
            <button className="button button--primary" type="button" onClick={begin}>
              {draft.walkthrough ? "Resume basic walkthrough" : "Open basic walkthrough"}
            </button>
            <button
              className="button button--quiet"
              type="button"
              onClick={() => setView("advanced")}
            >
              Open advanced editor
            </button>
          </div>
        </div>
      </section>
    );

  if (currentView === "basic") {
    const error = section ? sectionError(draft, section.key) : null;
    const confirmed = section ? progress.answers[section.key] : undefined;
    const complete =
      SECTIONS.every((item) => progress.answers[item.key]) &&
      sectionError(draft, "roles") === null &&
      !invalidPlaces &&
      !invalidPay &&
      !hasNumberErrors(draft);
    return (
      <section aria-labelledby="search-walkthrough-heading" className="search-setup">
        <section className="panel">
          <header>
            <div>
              <h2
                id="search-walkthrough-heading"
                ref={walkthroughHeading}
                tabIndex={-1}
                aria-describedby="search-walkthrough-progress"
              >
                Basic search walkthrough
              </h2>
              <p>
                Free setup. No CV or provider key needed. Answer each section or explicitly choose
                No restriction, then review before saving.
              </p>
              <p id="search-walkthrough-progress" role="status">
                Step {step + 1} of {SECTIONS.length + 1}: {section?.title ?? "Review"}
              </p>
            </div>
            <button
              className="button button--quiet"
              type="button"
              disabled={save.isPending}
              onClick={() => {
                setEdits({ ...draft, walkthrough: { ...progress, paused: true } });
                setView("paused");
                onSkip?.();
              }}
            >
              Skip for now
            </button>
          </header>
          <div className="panel-body">
            <p>
              Unsaved answers stay in this browser tab. No preference takes effect before the
              confirmed save.
            </p>
            {storageError && (
              <p role="alert">
                Browser draft storage is unavailable. Keep this page open to preserve your
                unfinished answers.
              </p>
            )}
            {save.isError && <p role="alert">{save.error.message}</p>}
          </div>
        </section>
        {recoveryPanel}
        <datalist id="country-list">
          {COUNTRIES.map((country) => (
            <option key={country} value={country} />
          ))}
        </datalist>
        {section && (
          <section className="panel">
            <header>
              <h2>{section.title}</h2>
            </header>
          </section>
        )}
        {step === 0 && (
          <>
            <Level
              basic
              draft={draft}
              patch={patch}
              employmentTypes={options.data.employment_types}
              lanes={options.data.lanes}
              levels={options.data.levels}
            />
            <Focus basic caps={options.data.caps} draft={draft} patch={patch} />
          </>
        )}
        {step === 1 && (
          <Places
            basic
            draft={draft}
            patch={patch}
            acceptance={options.data.acceptance}
            eligibility={options.data.eligibility}
          />
        )}
        {step === 2 && (
          <Working basic draft={draft} patch={patch} acceptance={options.data.acceptance} />
        )}
        {step === 3 && <Pay basic bases={options.data.bases} draft={draft} patch={patch} />}
        {step === SECTIONS.length && (
          <>
            <SearchPreferenceReview draft={draft} />
            <OpportunityThesis basic draft={draft} patch={patch} />
          </>
        )}
        <section className="panel">
          <div className="panel-body">
            {section && (
              <>
                <p>No restriction in this section: {section.scope}</p>
                <div className="button-row">
                  <button
                    className="button button--quiet"
                    type="button"
                    disabled={Boolean(error)}
                    onClick={() =>
                      setEdits({
                        ...draft,
                        walkthrough: {
                          ...progress,
                          answers: { ...progress.answers, [section.key]: "stated" },
                        },
                      })
                    }
                  >
                    Use these answers
                  </button>
                  <button
                    className="button button--quiet"
                    type="button"
                    onClick={() =>
                      setEdits({
                        ...withoutBasicRestrictions(draft, section.key),
                        walkthrough: {
                          ...progress,
                          answers: {
                            ...progress.answers,
                            [section.key]: "unrestricted",
                            ...(section.key === "places" ? { pay: undefined } : {}),
                          },
                        },
                      })
                    }
                  >
                    No restriction in this section
                  </button>
                </div>
                {error && (confirmed !== "unrestricted" || section.key === "roles") && (
                  <p className="field-note">{error}</p>
                )}
                {confirmed && (
                  <p role="status">
                    {confirmed === "stated"
                      ? "Answers confirmed for this section. Blank fields remain unstated."
                      : "No basic restriction confirmed. Any retained advanced preferences appear in review."}
                  </p>
                )}
              </>
            )}
            <div className="button-row">
              <button
                className="button button--quiet"
                type="button"
                disabled={step === 0 || save.isPending}
                onClick={() => move(step - 1)}
              >
                Back
              </button>
              {section ? (
                <button
                  className="button button--primary"
                  type="button"
                  disabled={!confirmed || (section.key === "roles" && Boolean(error))}
                  onClick={() => move(step + 1)}
                >
                  Next
                </button>
              ) : (
                <button
                  className="button button--primary"
                  type="button"
                  disabled={!complete || save.isPending || conflicted}
                  onClick={saveDraft}
                >
                  {save.isPending ? "Saving" : "Confirm and save preferences"}
                </button>
              )}
            </div>
          </div>
        </section>
      </section>
    );
  }

  return (
    <section aria-labelledby="search-setup-heading" className="search-setup">
      <section className="panel">
        <header>
          <div>
            <h2 id="search-setup-heading">Search setup</h2>
            <p>
              {serverVersion === 0
                ? "Nothing saved yet, so this workspace has stated no preferences and no job is ruled out by them."
                : `Version ${serverVersion}, effective ${new Date(stored.data?.effective_at ?? "").toLocaleString()}.`}
            </p>
          </div>
          <div className="button-row">
            <button className="button button--quiet" type="button" onClick={begin}>
              {draft.walkthrough ? "Resume basic walkthrough" : "Open basic walkthrough"}
            </button>
            {dirty && (
              <span className="field-note">
                Unsaved changes - kept in this browser tab until saved or discarded.
              </span>
            )}
            <ConfirmedActionButton
              className="button button--danger"
              disabled={!dirty || save.isPending}
              onConfirm={() => {
                setEdits(null);
                reset();
              }}
              options={{
                title: "Discard these search changes?",
                description:
                  "The saved search parameters stay in force. Changes made since the last save are lost.",
                confirmLabel: "Discard changes",
                danger: true,
              }}
            >
              Discard changes
            </ConfirmedActionButton>
            <button
              className="button button--primary"
              disabled={
                !dirty ||
                save.isPending ||
                conflicted ||
                hasNumberErrors(draft) ||
                invalidPlaces ||
                invalidPay
              }
              onClick={saveDraft}
              type="button"
            >
              {save.isPending ? "Saving" : "Save as new version"}
            </button>
          </div>
        </header>
        <div className="panel-body">
          {storageError && (
            <p role="alert">
              Browser draft storage is unavailable. Save your changes before leaving this form.
            </p>
          )}
          {save.isSuccess && !dirty && !save.data?.unchanged && (
            <p role="status">Search parameters saved.</p>
          )}
          {save.isError && <p className="field-error">{save.error.message}</p>}
          {save.data?.unchanged && (
            <p role="status">Identical to version {save.data.version}; nothing was written.</p>
          )}
        </div>
      </section>

      {recoveryPanel}
      <section aria-labelledby="target-titles-heading" className="panel">
        <header>
          <div>
            <h2 id="target-titles-heading">Target titles</h2>
            <p>Board scans look for these titles.</p>
          </div>
        </header>
        <div className="panel-body">
          <TargetTitles
            lanes={options.data?.lanes ?? []}
            onChange={(values) =>
              patch({ seniority: { ...draft.seniority, target_titles: values } })
            }
            values={draft.seniority.target_titles ?? []}
          />
        </div>
      </section>
      <datalist id="country-list">
        {COUNTRIES.map((country) => (
          <option key={country} value={country} />
        ))}
      </datalist>
      <section className="panel">
        <header>
          <div>
            <h2>Optional guided search</h2>
            <p>
              Target titles and preferences below drive discovery. With a provider key and CV,
              guided search can suggest evidence-backed role families and adjacent directions.
            </p>
          </div>
        </header>
        <div className="panel-body">
          <p>
            Explicit refusals and exclusions can rule postings out. Preferred and acceptable values
            steer choices. Blank values remain unstated; they do not become refusals. Search changes
            take effect only after Save as new version.
          </p>
          <a className="button button--quiet" href="/find?tab=web">
            Find roles from my background
          </a>
        </div>
      </section>
      <OpportunityThesis draft={draft} patch={patch} />
      <details className="panel">
        <summary>Where and how you can work</summary>
        <Working acceptance={options.data?.acceptance ?? []} draft={draft} patch={patch} />
        <Places
          acceptance={options.data?.acceptance ?? []}
          draft={draft}
          eligibility={options.data?.eligibility ?? []}
          patch={patch}
        />
      </details>
      <details className="panel">
        <summary>Compensation needs</summary>
        <Pay bases={options.data?.bases ?? []} draft={draft} patch={patch} />
      </details>
      <details className="panel">
        <summary>Role preferences and exclusions</summary>
        <Level
          draft={draft}
          employmentTypes={options.data?.employment_types ?? []}
          lanes={options.data?.lanes ?? []}
          levels={options.data?.levels ?? []}
          patch={patch}
          targetTitles={false}
        />
        <Focus caps={options.data.caps} draft={draft} patch={patch} />
      </details>
      <details className="panel">
        <summary>Advanced search controls</summary>
        <p className="field-note">
          Country aliases change how publisher text is interpreted. Leave them empty to use the
          shipped country vocabulary.
        </p>
        <CountryTable draft={draft} patch={patch} />
      </details>
    </section>
  );
}
