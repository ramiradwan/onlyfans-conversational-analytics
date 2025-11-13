import React from 'react';  
import Snackbar from '@mui/material/Snackbar';  
import Alert from '@mui/material/Alert';  
import Collapse from '@mui/material/Collapse';  
import IconButton from '@mui/material/IconButton';  
import InfoIcon from '@mui/icons-material/Info';  
import CloseIcon from '@mui/icons-material/Close';  
import { useChatStore } from '../store/useChatStore';  
import { useTheme } from '@mui/material/styles';  
import { useDebugStore } from '../store/useDebugStore';  
  
export function ErrorSnackbar() {  
  const theme = useTheme();  
  const { open, severity, message, details } = useChatStore(s => s.snackbar);  
  const closeSnackbar = useChatStore(s => s.closeSnackbar);  
  const addLog = useDebugStore(s => s.addLog);  
  const [showDetails, setShowDetails] = React.useState(false);  
  
  // Log to debug store when error appears  
  React.useEffect(() => {  
    if (open && severity === 'error') {  
      addLog('error', `${message} ${details || ''}`);  
    }  
  }, [open, severity, message, details, addLog]);  
  
  if (severity !== 'error') return null;  
  
  return (  
    <Snackbar  
      open={open}  
      autoHideDuration={4000}  
      onClose={closeSnackbar}  
      anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}  
      sx={{  
        mb: theme.spacing(showDetails ? 8 : 2),  
      }}  
    >  
      <Alert  
        severity="error"  
        variant="filled"  
        onClose={closeSnackbar}  
        sx={{  
          width: '100%',  
          bgcolor: theme.palette.error.light,  
          color: theme.palette.getContrastText(theme.palette.error.light),  
        }}  
        icon={<InfoIcon />}  
        aria-live="assertive"  
        action={  
          details ? (  
            <IconButton  
              aria-label="Show error details"  
              aria-expanded={showDetails}  
              color="inherit"  
              size="small"  
              onClick={() => setShowDetails(prev => !prev)}  
            >  
              {showDetails ? <CloseIcon /> : <InfoIcon />}  
            </IconButton>  
          ) : undefined  
        }  
      >  
        {message || 'An unexpected error occurred'}  
        {details && (  
          <Collapse in={showDetails} unmountOnExit>  
            <pre  
              style={{  
                marginTop: theme.spacing(1),  
                whiteSpace: 'pre-wrap',  
                fontSize: theme.typography.caption.fontSize,  
              }}  
            >  
              {details}  
            </pre>  
          </Collapse>  
        )}  
      </Alert>  
    </Snackbar>  
  );  
}  