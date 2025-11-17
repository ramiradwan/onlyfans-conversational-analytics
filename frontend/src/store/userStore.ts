import { create } from 'zustand';

// Per Spec 3.1: Roles are 'Creator-CEO' and 'Operator'.
// We use 'operator' for the type.
type UserRole = 'creator-ceo' | 'operator' | null;

interface UserStoreState {
  role: UserRole;
  actions: {
    setUserRole: (role: UserRole) => void;
  };
}

export const useUserStore = create<UserStoreState>((set) => ({
  // Hardcoded as 'creator-ceo' for scaffolding per Spec 3.1/11.2
  role: 'creator-ceo',
  actions: {
    setUserRole: (role) => set({ role }),
  },
}));

// Export a selector for the role
export const useUserRole = () => useUserStore((state) => state.role);

// Export actions for easy access
export const useUserActions = () => useUserStore((state) => state.actions);