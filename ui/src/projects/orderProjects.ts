/**
 * The one Project ordering shared by the Plus_Menu and the Projects_Surface
 * (Req 3.2, 10.5): the Personal_Project first, then every other Project by
 * `localeCompare(…, {sensitivity: "base"})` on its name, so case and accent
 * variants sort together.
 *
 * Pure and non-mutating: returns a new array. `Array.prototype.sort` is stable
 * (ES2019+), so Projects whose names compare equal keep their input order.
 */

/** The fields the ordering reads; callers may pass richer Project rows. */
export interface OrderableProject {
  name: string;
  personal: boolean;
}

export function compareProjectNames(a: string, b: string): number {
  return a.localeCompare(b, undefined, { sensitivity: "base" });
}

export function orderProjects<T extends OrderableProject>(projects: readonly T[]): T[] {
  return [...projects].sort((a, b) => {
    if (a.personal !== b.personal) return a.personal ? -1 : 1;
    return compareProjectNames(a.name, b.name);
  });
}
