import { useState } from "react";
import { Link } from "react-router-dom";

import { useTitleExclusionCollateral } from "../../api/hooks";
import { useWorkspace } from "../../providers";
import { employmentTypeLabel } from "../../components/text";
import { type SectionProps } from "./draft";
import { TargetTitles } from "./TargetTitles";
import { TokenField } from "./TokenField";

export function Level({
  basic = false,
  draft,
  employmentTypes,
  lanes,
  levels,
  patch,
  targetTitles = true,
}: SectionProps & {
  basic?: boolean;
  targetTitles?: boolean;
  employmentTypes: string[];
  lanes: string[];
  levels: string[];
}) {
  const [archetypeKeys] = useState(() => new WeakMap<object, string>());
  const archetypeKey = (item: object) => {
    let key = archetypeKeys.get(item);
    if (!key) {
      key = crypto.randomUUID();
      archetypeKeys.set(item, key);
    }
    return key;
  };
  const updateArchetype = (
    index: number,
    changes: Partial<NonNullable<SectionProps["draft"]["employers"]["archetypes"]>[number]>,
  ) =>
    patch({
      employers: {
        ...draft.employers,
        archetypes: (draft.employers.archetypes ?? []).map((saved, position) => {
          if (position !== index) return saved;
          const edited = { ...saved, ...changes };
          archetypeKeys.set(edited, archetypeKey(saved));
          return edited;
        }),
      },
    });
  return (
    <article className="panel">
      <header>
        <div>
          <h2>What role you want</h2>
          <p>
            Title-inferred levels outside this band need review. Explicit title exclusions still
            apply.
          </p>
        </div>
      </header>
      <div className="panel-body settings-grid">
        {(["minimum", "maximum"] as const).map((key) => (
          <label className="compact-field" key={key}>
            {key === "minimum" ? "Lowest preferred level" : "Highest preferred level"}
            <select
              onChange={(event) =>
                patch({ seniority: { ...draft.seniority, [key]: event.target.value } })
              }
              value={draft.seniority[key] ?? ""}
            >
              <option value="">Not stated</option>
              {levels.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label className="compact-field">
          How the target titles are used
          <select
            onChange={(event) =>
              patch({
                seniority: {
                  ...draft.seniority,
                  title_matching: event.target.value as "guided" | "strict",
                },
              })
            }
            value={draft.seniority.title_matching ?? "guided"}
          >
            <option value="guided">Steer the search, but do not exclude other titles</option>
            <option value="strict">Only postings whose title matches the list</option>
          </select>
          <small className="field-note">Nobody knows every name their own job goes by.</small>
        </label>
        {targetTitles && (
          <TargetTitles
            basic={basic}
            lanes={lanes}
            onChange={(values) =>
              patch({ seniority: { ...draft.seniority, target_titles: values } })
            }
            values={draft.seniority.target_titles ?? []}
          />
        )}
        <TokenField
          hint="Blank excludes nothing."
          label="Titles to exclude"
          onChange={(values) =>
            patch({ seniority: { ...draft.seniority, excluded_titles: values } })
          }
          placeholder="Type a title, then press Enter"
          values={draft.seniority.excluded_titles ?? []}
        />
        <ExclusionCollateral phrases={draft.seniority.excluded_titles ?? []} />
        <EmploymentTypes
          known={employmentTypes}
          onChange={(values) => patch({ employment: { ...draft.employment, types: values } })}
          values={draft.employment.types ?? []}
        />
        <TokenField
          hint="These engagement types are excluded when a posting states them."
          label="Employment types to exclude"
          onChange={(values) =>
            patch({ employment: { ...draft.employment, excluded_types: values } })
          }
          placeholder="Type an employment type, then press Enter"
          values={draft.employment.excluded_types ?? []}
        />
        {!basic && (
          <details>
            <summary>
              Advanced preferences{" "}
              <span className="field-note">
                payroll, industries, employer sizes:{" "}
                {savedCount([
                  draft.employment.payroll_arrangements,
                  draft.employers.industries,
                  draft.employers.excluded_industries,
                  draft.employers.sizes,
                ])}
              </span>
            </summary>
            <TokenField
              hint="Guides discovery and evaluation. ClearScout only treats it as a hard rule when the posting states its payroll arrangement."
              label="Payroll arrangements you can accept"
              onChange={(values) =>
                patch({ employment: { ...draft.employment, payroll_arrangements: values } })
              }
              placeholder="For example, employee payroll"
              values={draft.employment.payroll_arrangements ?? []}
            />
            <TokenField
              hint="Guides which organisations and postings discovery explores."
              label="Industries of interest"
              onChange={(values) =>
                patch({ employers: { ...draft.employers, industries: values } })
              }
              placeholder="Type an industry, then press Enter"
              values={draft.employers.industries ?? []}
            />
            <TokenField
              hint="Guides discovery away from these industries. Blank excludes none."
              label="Industries to exclude"
              onChange={(values) =>
                patch({ employers: { ...draft.employers, excluded_industries: values } })
              }
              placeholder="Type an industry, then press Enter"
              values={draft.employers.excluded_industries ?? []}
            />
            <TokenField
              hint="Describe the employer scales worth considering in your own words."
              label="Employer sizes to consider"
              onChange={(values) => patch({ employers: { ...draft.employers, sizes: values } })}
              placeholder="For example, growth-stage or enterprise"
              values={draft.employers.sizes ?? []}
            />
          </details>
        )}
        {!basic && (
          <>
            <div className="compact-field">
              <span>Employer types and named associations</span>
              {(draft.employers.archetypes ?? []).length === 0 && (
                <small className="field-note">
                  None saved yet. Add one here or accept a search suggestion.
                </small>
              )}
              {(draft.employers.archetypes ?? []).map((item, index) => (
                <details key={archetypeKey(item)}>
                  <summary>{item.name.trim() || `Unfinished employer type ${index + 1}`}</summary>
                  <fieldset>
                    <legend>Employer type {index + 1}</legend>
                    <label className="compact-field">
                      Employer type name
                      <input
                        aria-invalid={!item.name.trim() && (item.employer_names ?? []).length > 0}
                        aria-errormessage={`${archetypeKey(item)}-error`}
                        maxLength={120}
                        value={item.name}
                        onChange={(event) => updateArchetype(index, { name: event.target.value })}
                      />
                    </label>
                    <label className="compact-field">
                      Why this employer type matters
                      <input
                        maxLength={300}
                        value={item.because}
                        onChange={(event) =>
                          updateArchetype(index, { because: event.target.value })
                        }
                      />
                    </label>
                    <TokenField
                      label={`Employers in type ${index + 1}`}
                      hint="Use exact publisher or saved employer-board names. A configured role phrase plus this association allows a body read for review. It does not establish eligibility or override exclusions."
                      placeholder="Type an employer, then press Enter"
                      values={item.employer_names ?? []}
                      onChange={(values) => updateArchetype(index, { employer_names: values })}
                    />
                    {!item.name.trim() && (item.employer_names ?? []).length > 0 && (
                      <small id={`${archetypeKey(item)}-error`} role="alert">
                        Name the employer type before associating employers.
                      </small>
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        patch({
                          employers: {
                            ...draft.employers,
                            archetypes: (draft.employers.archetypes ?? []).filter(
                              (_, position) => position !== index,
                            ),
                          },
                        })
                      }
                    >
                      Remove employer type {index + 1}
                    </button>
                  </fieldset>
                </details>
              ))}
              <button
                type="button"
                onClick={() =>
                  patch({
                    employers: {
                      ...draft.employers,
                      archetypes: [
                        ...(draft.employers.archetypes ?? []),
                        { name: "", because: "", employer_names: [] },
                      ],
                    },
                  })
                }
              >
                Add employer type
              </button>
            </div>
          </>
        )}
        <TokenField
          hint="Blank excludes nothing."
          label="Employers to exclude"
          onChange={(values) =>
            patch({ employers: { ...draft.employers, excluded_employers: values } })
          }
          placeholder="Type an employer, then press Enter"
          values={draft.employers.excluded_employers ?? []}
        />
        {!basic && (
          <details>
            <summary>
              Advanced matching{" "}
              <span className="field-note">
                role nouns, domain tokens:{" "}
                {savedCount([
                  draft.seniority.specialist_ic_role_nouns,
                  draft.seniority.strong_domain_tokens,
                ])}
              </span>
            </summary>
            <p className="field-note">
              These controls change review routing. They are optional; blank specialist terms
              disable that extra review lane. The scoring policy is chosen under{" "}
              <Link to="/find/scoring">How jobs are scored</Link>.
            </p>
            <TokenField
              hint="A saved role phrase keeps matching titles for a body read and review. An explicit employer association adds domain context to that review. Strict title matching and explicit exclusions still apply. Clear this list to disable the specialist review lane."
              label="Specialist individual-contributor role nouns"
              onChange={(values) =>
                patch({ seniority: { ...draft.seniority, specialist_ic_role_nouns: values } })
              }
              placeholder="Type a role noun, then press Enter"
              values={draft.seniority.specialist_ic_role_nouns ?? []}
            />
            <TokenField
              hint="Matching postings stay in review; they do not become target-title matches. Use precise terms to limit review noise."
              label="Strong domain tokens"
              onChange={(values) =>
                patch({ seniority: { ...draft.seniority, strong_domain_tokens: values } })
              }
              placeholder="Type a domain token, then press Enter"
              values={draft.seniority.strong_domain_tokens ?? []}
            />
          </details>
        )}
      </div>
    </article>
  );
}

/** Say how many of a collapsed section's fields hold a saved value. */
function savedCount(fields: (string | string[] | null | undefined)[]) {
  const saved = fields.filter((value) => (value?.length ?? 0) > 0).length;
  return saved === 0 ? "none saved" : `${saved} of ${fields.length} saved`;
}

/** Show what each draft title exclusion would cost in the Pending queue as it stands. */
function ExclusionCollateral({ phrases }: { phrases: string[] }) {
  const { workspaceId } = useWorkspace();
  const collateral = useTitleExclusionCollateral(workspaceId, phrases);
  if (phrases.length === 0) return null;
  const rows = collateral.data?.phrases ?? [];
  return (
    <div className="compact-field">
      <span>What these exclusions would hide</span>
      {collateral.isError && (
        <small className="field-note">
          The Pending queue could not be counted. The exclusions themselves are unaffected.
        </small>
      )}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table aria-label="Titles to exclude and what each would hide" className="data-table">
            <thead>
              <tr>
                <th scope="col">Title to exclude</th>
                <th scope="col">Pending rows it would hide</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.phrase}>
                  <th scope="row">{row.phrase}</th>
                  <td>{row.matched_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <small className="field-note">
        Counted against the {collateral.data?.queue_count ?? 0} rows in the Pending queue right now.
        Postings found after this is saved are not counted.
      </small>
    </div>
  );
}

/** Show picker values alongside valid custom employment types from file input. */
function EmploymentTypes({
  known,
  onChange,
  values,
}: {
  known: string[];
  onChange: (values: string[]) => void;
  values: string[];
}) {
  const custom = values.filter((value) => !known.includes(value));
  const toggle = (value: string, checked: boolean) =>
    onChange(checked ? [...values, value] : values.filter((item) => item !== value));
  return (
    <div className="compact-field">
      <span>Employment types</span>
      <div className="checkbox-list">
        {known.map((value) => (
          <label key={value}>
            <input
              checked={values.includes(value)}
              onChange={(event) => toggle(value, event.target.checked)}
              type="checkbox"
            />
            {employmentTypeLabel(value)}
          </label>
        ))}
      </div>
      {custom.length > 0 && (
        <ul className="token-list">
          {custom.map((value) => (
            <li key={value}>
              <span>{value}</span>
            </li>
          ))}
        </ul>
      )}
      <small className="field-note">
        Selecting more than one widens the posting labels ClearScout will accept as a match.
      </small>
    </div>
  );
}
