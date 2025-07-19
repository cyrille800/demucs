export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS headers
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    };

    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    try {
      if (path === '/generate-upload-link' && request.method === 'POST') {
        return await handleGenerateLink(request, env, corsHeaders);
      } else if (path.startsWith('/upload/') && request.method === 'POST') {
        return await handleUpload(request, env, corsHeaders);
      } else {
        return new Response('Not Found', { 
          status: 404, 
          headers: corsHeaders 
        });
      }
    } catch (error) {
      console.error('Error:', error);
      return new Response(JSON.stringify({ 
        error: 'Internal Server Error',
        message: error.message 
      }), {
        status: 500,
        headers: { 
          'Content-Type': 'application/json',
          ...corsHeaders 
        }
      });
    }
  }
};

async function handleGenerateLink(request, env, corsHeaders) {
  const body = await request.json();
  const { maxSizeBytes, allowedExtension, expiresInMinutes = 30 } = body;

  // Validation des paramètres
  if (!maxSizeBytes || !allowedExtension) {
    return new Response(JSON.stringify({
      error: 'Missing required parameters',
      message: 'maxSizeBytes and allowedExtension are required'
    }), {
      status: 400,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  // Générer un token unique
  const token = generateUniqueToken();
  
  // Créer les métadonnées du lien
  const linkData = {
    maxSizeBytes,
    allowedExtension: allowedExtension.toLowerCase(),
    createdAt: Date.now(),
    expiresAt: Date.now() + (expiresInMinutes * 60 * 1000),
    used: false
  };

  // Stocker dans KV
  await env.UPLOAD_LINKS.put(
    token, 
    JSON.stringify(linkData), 
    { expirationTtl: expiresInMinutes * 60 }
  );

  // Générer l'URL d'upload
  const uploadUrl = `${new URL(request.url).origin}/upload/${token}`;

  return new Response(JSON.stringify({
    success: true,
    uploadUrl,
    token,
    expiresAt: new Date(linkData.expiresAt).toISOString(),
    maxSizeBytes,
    allowedExtension
  }), {
    status: 200,
    headers: { 
      'Content-Type': 'application/json',
      ...corsHeaders 
    }
  });
}

async function handleUpload(request, env, corsHeaders) {
  const url = new URL(request.url);
  const token = url.pathname.split('/')[2];

  // Récupérer les données du lien
  const linkDataStr = await env.UPLOAD_LINKS.get(token);
  
  if (!linkDataStr) {
    return new Response(JSON.stringify({
      error: 'Invalid or expired upload link'
    }), {
      status: 404,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  const linkData = JSON.parse(linkDataStr);

  // Vérifier si déjà utilisé
  if (linkData.used) {
    return new Response(JSON.stringify({
      error: 'Upload link already used'
    }), {
      status: 410,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  // Parse du fichier
  const formData = await request.formData();
  const file = formData.get('file');

  if (!file) {
    return new Response(JSON.stringify({
      error: 'No file provided'
    }), {
      status: 400,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  // Vérifications
  if (file.size > linkData.maxSizeBytes) {
    return new Response(JSON.stringify({
      error: 'File too large'
    }), {
      status: 413,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  const fileExtension = file.name.split('.').pop()?.toLowerCase();
  if (fileExtension !== linkData.allowedExtension) {
    return new Response(JSON.stringify({
      error: 'Invalid file extension'
    }), {
      status: 415,
      headers: { 
        'Content-Type': 'application/json',
        ...corsHeaders 
      }
    });
  }

  // Marquer comme utilisé
  linkData.used = true;
  await env.UPLOAD_LINKS.put(token, JSON.stringify(linkData));

  // Stocker le fichier
  const uniqueFilename = `${token}_${Date.now()}_${file.name}`;
  await env.DUBBING_BUCKET.put(uniqueFilename, file.stream());

  return new Response(JSON.stringify({
    success: true,
    filename: uniqueFilename,
    originalName: file.name,
    size: file.size
  }), {
    status: 200,
    headers: { 
      'Content-Type': 'application/json',
      ...corsHeaders 
    }
  });
}

function generateUniqueToken() {
  const timestamp = Date.now().toString(36);
  const randomPart = Math.random().toString(36).substring(2, 15);
  return `${timestamp}_${randomPart}`;
}
