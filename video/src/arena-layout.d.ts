export type LogoBox = { x: number; y: number; w: number; h: number };

export type DefenseLike = { id: string };

export type DefensePosition = { x: number; y: number };

export type DefenseLayout = {
  positions: Record<string, DefensePosition>;
  nodeSize: number;
  rows: number;
  minGap: number;
};

export declare const BASE_NODE_SIZE: number;
export declare const MIN_GAP: number;

export declare function computeDefenseLayout(
  defenses: readonly DefenseLike[],
  width: number,
  height: number,
  logoBoxes: readonly LogoBox[],
): DefenseLayout;
