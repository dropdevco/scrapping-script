export type CategoryGroup = {
  label: string;
  aliases: string[];
};

export const CATEGORY_GROUPS: CategoryGroup[] = [
  {
    label: "Music",
    aliases: ["Music", "Concerts", "Rock", "Live Music", "Jazz", "Open Mic", "Karaoke"],
  },
  {
    label: "Sports",
    aliases: [
      "Sports",
      "Sports and Fitness",
      "Baseball",
      "Minor League",
      "Fitness",
      "Run",
      "Cycling",
      "Golf",
      "Tennis",
      "Volleyball",
    ],
  },
  {
    label: "Arts & Theatre",
    aliases: [
      "Arts & Theatre",
      "Arts and Theatre",
      "Performing Arts",
      "Theatre",
      "Theater",
      "Dance",
      "Comedy",
      "Art",
      "Gallery",
      "Art Exhibitions",
    ],
  },
  {
    label: "Family",
    aliases: ["Family", "Family-Friendly", "Friendly", "Kids", "Sensory Friendly Hours"],
  },
  {
    label: "Food & Drink",
    aliases: [
      "Food & Drink",
      "Food and Drink",
      "Food and Drinks",
      "Food",
      "Drink",
      "Wine",
      "Beer",
      "Dining",
      "Restaurants",
    ],
  },
  {
    label: "Festivals",
    aliases: ["Festivals", "Annual Events", "Festival", "Fairs", "Fair"],
  },
  {
    label: "Exhibits",
    aliases: ["Exhibits", "Exhibitions", "Museum and Exhibits", "Museum", "Museums"],
  },
  {
    label: "History & Culture",
    aliases: [
      "History & Culture",
      "History and Culture",
      "Culture",
      "Cultural",
      "Guided Tours",
      "Tours",
    ],
  },
  {
    label: "Film",
    aliases: ["Film", "Film and Video", "Movies", "Movie", "Screening"],
  },
  {
    label: "Classes & Workshops",
    aliases: ["Classes & Workshops", "Classes and Workshops", "Workshops", "Class", "Workshop"],
  },
  {
    label: "Community",
    aliases: ["Community", "Meetup", "Networking", "Civic", "Religion", "Spirituality"],
  },
  {
    label: "Tech",
    aliases: ["Tech", "Technology", "AI", "Software", "Startup", "Hackathon"],
  },
  {
    label: "Free",
    aliases: ["Free", "Free Event"],
  },
  {
    label: "Other",
    aliases: ["Other"],
  },
];

export const CANONICAL_CATEGORIES = CATEGORY_GROUPS.map((group) => group.label);

const aliasToLabel = new Map<string, string>();
for (const group of CATEGORY_GROUPS) {
  aliasToLabel.set(normalizeCategoryKey(group.label), group.label);
  for (const alias of group.aliases) {
    aliasToLabel.set(normalizeCategoryKey(alias), group.label);
  }
}

function normalizeCategoryKey(category: string): string {
  return category
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/&/g, "and")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

export function canonicalCategory(category: string): string {
  const key = normalizeCategoryKey(category);
  const exact = aliasToLabel.get(key);
  if (exact) return exact;

  if (/\b(concert|music|band|karaoke|jazz|rock|dj)\b/.test(key)) return "Music";
  if (/\b(sport|baseball|basketball|fitness|run|cycling|marathon|tournament)\b/.test(key)) {
    return "Sports";
  }
  if (/\b(theatre|theater|performing|dance|comedy|gallery|art)\b/.test(key)) {
    return "Arts & Theatre";
  }
  if (/\b(family|kids|children|sensory)\b/.test(key)) return "Family";
  if (/\b(food|drink|wine|beer|dinner|brunch|restaurant)\b/.test(key)) return "Food & Drink";
  if (/\b(festival|annual|fair)\b/.test(key)) return "Festivals";
  if (/\b(exhibit|museum)\b/.test(key)) return "Exhibits";
  if (/\b(history|culture|cultural)\b/.test(key)) return "History & Culture";
  if (/\b(film|movie|video|screening)\b/.test(key)) return "Film";
  if (/\b(workshop|class|lesson)\b/.test(key)) return "Classes & Workshops";
  if (/\b(tech|technology|software|startup|hackathon|ai)\b/.test(key)) return "Tech";
  if (/\b(free)\b/.test(key)) return "Free";

  return "Other";
}

export function canonicalCategoriesForEvent(categories: string[] | null | undefined): string[] {
  const seen = new Set<string>();
  for (const category of categories ?? []) {
    const clean = category.trim();
    if (!clean) continue;
    seen.add(canonicalCategory(clean));
  }
  return [...seen];
}

export function expandedCategoryAliases(categories: string[] | null | undefined): string[] {
  const expanded = new Set<string>();

  for (const category of categories ?? []) {
    const selected = category.trim();
    if (!selected) continue;
    expanded.add(selected);

    const group = CATEGORY_GROUPS.find((candidate) => candidate.label === canonicalCategory(selected));
    if (!group) continue;
    expanded.add(group.label);
    for (const alias of group.aliases) expanded.add(alias);
  }

  return [...expanded];
}
