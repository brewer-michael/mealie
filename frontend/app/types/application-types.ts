export interface SideBarLink {
  key?: string;
  icon: string;
  to?: string;
  href?: string;
  title: string;
  children?: SideBarLink[];
  childrenStartExpanded?: boolean;
  restricted: boolean;
  /** fork: a small badge after the title, e.g. recipe cards that failed to upload (docs/ai/PHASE2.md §1.1) */
  badge?: { content: number | string; color: string; label: string };
}

export type SidebarLinks = Array<SideBarLink>;
