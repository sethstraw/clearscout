import { FIND_TOUR } from "./content";

/** Runs the Find jobs tour from the help panel; Driver.js loads on first use. */
export async function startFindTour(): Promise<void> {
  const [{ driver }] = await Promise.all([
    import("driver.js"),
    import("driver.js/dist/driver.css"),
  ]);
  driver({
    allowClose: true,
    doneBtnText: "Done",
    nextBtnText: "Next",
    prevBtnText: "Back",
    showProgress: true,
    steps: FIND_TOUR.map(({ element, title, description }) => ({
      element,
      popover: { title, description },
    })),
  }).drive();
}
