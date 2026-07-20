import { create } from 'zustand';

// Per Spec 3.1: Roles are 'Creator-CEO' and 'Operator'.
// We use 'operator' for the type.
type UserRole = 'creator-ceo' | 'operator' | null;

interface UserStoreState {
  role: UserRole;
  // Distinguishes "not signed yet" (null, unresolved) from "signed with no role"
  // (null, resolved) so gated routes can wait instead of redirecting away.
  roleResolved: boolean;
  actions: {
    setUserRole: (role: UserRole) => void;
  };
}

export const useUserStore = create<UserStoreState>((set) => ({
  // The signed Brain session supplies the presentation role at bootstrap.
  role: null,
  roleResolved: false,
  actions: {
    setUserRole: (role) => set({ role, roleResolved: true }),
  },
}));

// Export a selector for the role
export const useUserRole = () => useUserStore((state) => state.role);

// Export a selector for whether the Brain session has reported a role yet
export const useRoleResolved = () => useUserStore((state) => state.roleResolved);

// Export actions for easy access
export const useUserActions = () => useUserStore((state) => state.actions);
